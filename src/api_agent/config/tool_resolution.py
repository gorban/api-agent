"""Join inert operation tooling to ordinary configuration resolution."""
from dataclasses import replace


def compose_operation_tools(context, schema, *, selection=None, argv=(), environ=None,
                            dotenv=None, overrides=None, operation_intents=(), definition_loader=None):
    """Legacy packs keep their schema; declaring tooling enables selected projection."""
    if (getattr(context, 'tool_catalog_path', None) is None
            and getattr(context, 'workflow_entry', None) is None
            and not (context.base_root / 'flow.md.html').is_file()):
        return None
    from api_agent.tooling.selection import compose_tools
    return compose_tools(context, schema, selection=selection, argv=argv,
                         environ=environ, dotenv=dotenv, overrides=overrides, operation_intents=operation_intents,
                         definition_loader=definition_loader)


def bind_operation_tools(plan, result, *, argv=()):
    """Preserve original input errors and distinguish known inactive options."""
    from api_agent.tooling.models import ToolSelectionError
    from .options import parse_options
    inactive = set(plan.inactive_fields)
    replacements = {
        'Unknown option --' + name.lower().replace('_', '-'):
        name + ': inactive for the selected workflow/test context'
        for name in inactive}
    # Resolve values against the active schema, but parse arity against the inert
    # catalog inventory so an inactive option value never becomes a test selector.
    active_options = parse_options(plan.schema, argv)
    inventory_options = parse_options(plan.option_schema, argv)
    errors = list(result.errors)
    for error in active_options.errors:
        if error in errors:
            errors.remove(error)
    collecting = plan.selection.mode == 'collection'
    for error in inventory_options.errors:
        if not collecting or not any(error.startswith(name + ':') for name in inactive):
            errors.append(error)
    if not collecting:
        errors.extend(replacements['Unknown option --' + name.lower().replace('_', '-')]
                      for name in inventory_options.values if name in inactive)
    result = replace(result, errors=tuple(errors), positionals=inventory_options.positionals)
    try:
        bound = plan.bind_values(result)
    except ToolSelectionError as error:
        return replace(result, errors=(*result.errors, str(error)))
    return replace(result, tool_plan=bound)
