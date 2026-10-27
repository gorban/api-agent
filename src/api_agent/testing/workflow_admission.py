"""Capture runner-held workflow identity before worker execution."""
from copy import deepcopy
from pathlib import Path


def capture_workflow_admissions(context, supplied=None, *, tool_plan=None, profiles=None):
    """Bind the selected definition without importing or executing native code.

    Explicit admissions belong to the trusted caller, never a worker packet.
    A test-specific graph without such admission remains unverified. Contexts
    with no selected or bundled graph need no workflow admission.
    """
    if supplied is not None:
        return deepcopy(supplied)
    if tool_plan is not None:
        definitions = tool_plan.definitions
    else:
        definitions = _selected_definitions(context)
    if profiles is None:
        from api_agent.workflows.journal import projection
        return {definition.definition_id: projection(definition) for definition in definitions}
    from api_agent.workflows.execution import resolve_component_passes
    from api_agent.workflows.journal import projection
    return {(test_id, definition.definition_id): projection(definition, component_passes=
                resolve_component_passes(definition.layout, row['config'])[0])
            for test_id, row in profiles.items() for definition in definitions
            if tool_plan is None or definition.definition_id in tool_plan.case_definitions.get(test_id, ())}


def _selected_definitions(context):
    entry = getattr(context, 'workflow_entry', None)
    if entry is None and not (Path(context.base_root) / 'flow.md.html').is_file():
        return ()
    from api_agent.runtime.resource_pack import load_selected_definition
    return (load_selected_definition(context),)
