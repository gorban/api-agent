"""Value-free diagnosis and non-destructive advice for managed infrastructure."""
import shlex


def missing_captured_assets_message(identifier, *, executable, project):
    """Explain a retained bind whose captured host files were removed."""
    border = '!' * 72
    notice = '\n'.join((
        border, '', 'ACTION REQUIRED: retained prerequisite files are missing', '',
        'A retained container references captured host files that no longer exist.',
        'Cleaning ignored artifacts (for example with git clean -xdf) can cause this.',
        'The container cannot be adopted until it is recreated with fresh captures.', '',
        'Recover while preserving named volumes:',
        f'1. Stop all launcher/test invocations using project {project}.',
        '2. Use the same container engine/context as the launcher. Confirm the',
        '   identified container is not being used by another operation.',
        '3. Stop and remove only that container:',
        '   ' + shlex.join((executable, 'stop', identifier)),
        '   ' + shlex.join((executable, 'rm', identifier)),
        '4. Rerun the launcher/test; it captures the assets and recreates the container.', '',
        'Keep all volumes. Do not use -v, compose down -v, or volume pruning.',
        '', border,
    ))
    return 'Prerequisite captured assets are missing.' + '\n\n\n' + notice + '\n\n'

_LANGFUSE_SETTINGS = {
    'LANGFUSE_INIT_USER_EMAIL': 'LANGFUSE_ADMIN_EMAIL',
    'LANGFUSE_INIT_USER_PASSWORD': 'LANGFUSE_ADMIN_PASSWORD',
}


def initialization_changes(service, actual, expected):
    """Compare in-memory inspection values; return only known setting names."""
    if service != 'langfuse-web' or type(actual) is not list:
        return ()
    values = {}
    for entry in actual:
        if type(entry) is not str or '=' not in entry:
            return ()
        key, value = entry.split('=', 1)
        if key in values:
            return ()
        values[key] = value
    return tuple(public for key, public in _LANGFUSE_SETTINGS.items()
                 if key in values and key in expected and values[key] != expected[key])


def incompatible_stack_message(item, *, configuration_changed, executable, project):
    message = 'Existing prerequisite stack is incompatible; review it before replacement.'
    if (item.service != 'langfuse-web' or item.profile != 'langfuse'
            or not configuration_changed):
        return message
    cause = ('Changed initialization settings:\n' + '\n'.join('  ' + name for name in item.initialization_changes)
             if item.initialization_changes else
             'Changing LANGFUSE_ADMIN_EMAIL or LANGFUSE_ADMIN_PASSWORD can cause this;\n'
             'the exact setting change could not be established. Other settings or images may differ.')
    border = '!' * 72
    notice = '\n'.join((
        border, '', 'ACTION REQUIRED: managed local LangFuse configuration changed', '',
        cause, 'The retained langfuse-web container does not match the current configuration.', '',
        'Recover while preserving traces:',
        f'1. Stop all Run local, trace viewer and test invocations using project {project}.',
        '2. Use the same container engine/context as the launcher. Confirm the identified',
        '   web container is stopped and no other operation is using this stack.',
        '3. Remove only that stopped container:',
        '   ' + shlex.join((executable, 'rm', item.id)),
        '4. Rerun the launcher/test; it recreates the web container with current settings.', '',
        'Keep all volumes. Do not use force, -v, compose down -v, or volume pruning.',
        'Existing traces remain in the retained volumes. This does not rotate an existing',
        'database account: use its actual login or change the account inside LangFuse too.',
        '', border,
    ))
    return message + '\n\n\n' + notice + '\n\n'
