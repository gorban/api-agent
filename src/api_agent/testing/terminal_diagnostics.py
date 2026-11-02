"""Deferred terminal disclosure of finalized successful-test streams."""
import json

from api_agent.providers.scrubbed_response import ResponseValidationError, scrub_response


def write_successful_diagnostics(tests, write, *, scrubber, level='warning'):
    """Write nonempty captures once, in selection order, after runner finalization.

    Runners own free-text protection and typed projection. Reapplying the legacy
    text matcher here would mask already projected JSON. Check the complete
    terminal record against the current registered credentials instead.
    """
    if level not in ('warning', 'info'):
        raise ValueError('SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL must be warning or info.')
    seen = set()
    for test in tests:
        if test.status != 'passed' or test.identity.id in seen:
            continue
        seen.add(test.identity.id)
        parts = []
        for name in ('stderr', 'stdout') if level == 'info' else ('stderr',):
            text = getattr(test, 'local_' + name, '')
            if text:
                parts.append(name + ':\n' + text.rstrip('\n'))
        if not parts:
            continue
        header = 'Test diagnostics: ' + json.dumps(test.identity.id, ensure_ascii=True) + ' — passed'
        with scrubber.boundary._authority.lock:
            _, snapshot = scrubber.boundary.snapshot_content()
            try:
                text, _ = scrub_response(header + '\n' + '\n\n'.join(parts),
                    schema={'type': 'string', 'minLength': 1}, secrets=tuple(snapshot._secrets),
                    operation='terminal_test_diagnostic')
            except ResponseValidationError:
                text = 'Test diagnostics withheld: credential protection.'
            write(text)
