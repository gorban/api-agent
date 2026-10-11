"""Startup intent validation shared by CLI and interactive entry points."""

START_MODES = frozenset({'run-local', 'unit', 'integration', 'e2e', 'allure', 'deployment-logs'})


def start_requested(values):
    """Whether startup should continue automatically after configuration."""
    return values.get('START') in {'True', 'E2E'}


def e2e_accepted(result):
    """Whether the user explicitly accepted E2E execution through START."""
    field = result.fields.get('START')
    return result.values.get('START') == 'E2E' and field is not None and field.explicit


def automatically_started(result):
    """Classify operations eligible for noninteractive execution."""
    values = result.values
    mode = values.get('TUI_MODE')
    return (mode in {'unit', 'run-local', 'allure', 'deployment-logs'}
            or (mode == 'integration' and start_requested(values))
            or (mode == 'e2e' and e2e_accepted(result)))


def start_configuration_errors(result):
    """Reject unsupported startup intent before any operation prerequisites run."""
    if result.help_requested:
        return ()
    values = result.values
    mode = values.get('TUI_MODE')
    if start_requested(values) and mode not in START_MODES:
        return ('START=True or E2E requires TUI_MODE to select run-local, unit, integration, e2e, allure, or deployment-logs.',)
    field = result.fields.get('START')
    if (field is not None and field.explicit and values.get('START') == 'False'
            and values.get('NO_STD_IN') and not values.get('COLLECT_ONLY')
            and automatically_started(result)):
        message = 'START=False conflicts with automatic execution using NO_STD_IN=True; omit START or set START=True'
        if mode in {'unit', 'integration', 'e2e'}:
            message += '; collection-only is still allowed via --collect-only'
        return (message + '.',)
    return ()
