"""Capture runner-held workflow identity before worker execution."""
from copy import deepcopy
from pathlib import Path


def capture_workflow_admissions(context, supplied=None):
    """Bind the selected definition without importing or executing native code.

    Explicit admissions belong to the trusted caller, never a worker packet.
    A test-specific graph without such admission remains unverified. Contexts
    with no selected or bundled graph need no workflow admission.
    """
    if supplied is not None:
        return deepcopy(supplied)
    entry = getattr(context, 'workflow_entry', None)
    if entry is None and not (Path(context.base_root) / 'flow.md.html').is_file():
        return {}
    from api_agent.runtime.resource_pack import load_selected_definition
    from api_agent.workflows.journal import projection
    definition = load_selected_definition(context)
    return {definition.definition_id: projection(definition)}
