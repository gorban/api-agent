"""Content permission is separate from credential protection and trace settings."""
import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.diagnostics import CredentialScrubber, disclose_text, result_diagnostics
from api_agent.testing.terminal_diagnostics import write_successful_diagnostics


@pytest.mark.parametrize('log_content', [False, True])
@pytest.mark.parametrize('status', ['passed', 'broken', 'failed'])
def test_terminal_projection_obeys_log_permission(log_content, status):
    identity = TestIdentity('sample::test.py::test_a', 'sample', 'test.py', 'test_a')
    narrative = 'narrative-sentinel'
    credential = 'credential-sentinel'
    result = TestResult(identity, status, traceback=narrative,
                        local_diagnostic=narrative if status != 'passed' else '',
                        local_stdout=narrative)
    scrubber = CredentialScrubber(secret_values=(credential,))
    output = []
    if status == 'passed':
        write_successful_diagnostics([result], output.append, scrubber=scrubber,
                                     level='info', allow_content=log_content)
    else:
        output.extend(scrubber(row) for row in result_diagnostics([result], allow_content=log_content))
    output.append(disclose_text(narrative + ' ' + credential, scrubber, allow_content=log_content))
    rendered = '\n'.join(output)
    assert (narrative in rendered) is log_content
    assert credential not in rendered
    assert identity.id in rendered


@pytest.mark.parametrize('allow_content', [False, True])
def test_terminal_captures_do_not_disclose_credentials(allow_content):
    identity = TestIdentity('sample::test.py::test_a', 'sample', 'test.py', 'test_a')
    secret = 'credential-sentinel'
    scrubber = CredentialScrubber(secret_values=(secret,))
    failed = TestResult(identity, 'failed', traceback=secret, local_diagnostic=secret,
                        local_stdout=secret, local_stderr=secret)
    passed = TestResult(identity, 'passed', local_stdout=secret, local_stderr=secret)
    output = [scrubber(row) for row in result_diagnostics([failed], allow_content=allow_content)]
    write_successful_diagnostics([passed], output.append, scrubber=scrubber,
                                 level='info', allow_content=allow_content)
    assert secret not in '\n'.join(output)


def test_untrusted_text_defaults_to_withheld():
    scrubber = CredentialScrubber(secret_values=('credential-sentinel',))
    assert 'narrative-sentinel' not in disclose_text('narrative-sentinel', scrubber)
    assert 'credential-sentinel' not in disclose_text('credential-sentinel', scrubber, allow_content=True)


def test_typed_selection_and_configuration_guidance_omits_untrusted_text():
    from api_agent.testing.collection import CollectionError, select_tests
    from api_agent.testing.diagnostics import RequiredConfigurationError, safe_exception

    scrubber = CredentialScrubber(secret_values=('HIDDEN_FIELD',))
    with pytest.raises(CollectionError) as caught:
        select_tests([], ['narrative-sentinel'])
    output = safe_exception(caught.value, scrubber)
    assert 'Unknown or empty test selector' in output
    assert 'narrative-sentinel' not in output
    assert safe_exception(RequiredConfigurationError(['VISIBLE_FIELD', 'HIDDEN_FIELD']), scrubber) == (
        'Missing required configuration: VISIBLE_FIELD, [REDACTED]')
    denied = safe_exception(ValueError('narrative-sentinel'), scrubber)
    assert 'invalid_value' in denied and 'narrative-sentinel' not in denied
    assert 'narrative-sentinel' in safe_exception(ValueError('narrative-sentinel'), scrubber, allow_content=True)
    with pytest.raises(ValueError):
        RequiredConfigurationError(['BAD\nINJECTED'])


@pytest.mark.parametrize('source,expected', [('dotenv', 'Clear .env configuration'),
                                             ('environment', 'Stop passing')])
def test_unattended_guidance_uses_fixed_classification(source, expected):
    from api_agent.providers.contracts import UnattendedCredentialError
    from api_agent.testing.diagnostics import safe_exception
    error = UnattendedCredentialError('narrative-sentinel', index_source=source)
    text = safe_exception(error, CredentialScrubber())
    assert expected in text and 'narrative-sentinel' not in text


def test_unattended_guidance_uses_resolved_source_only_when_error_has_none():
    from api_agent.providers.contracts import UnattendedCredentialError
    from api_agent.testing.diagnostics import safe_exception
    scrubber = CredentialScrubber()
    no_source = UnattendedCredentialError('private narrative')
    assert 'Clear .env configuration' in safe_exception(no_source, scrubber, unattended_index_source='dotenv')
    explicit = UnattendedCredentialError('private narrative', index_source='environment')
    assert 'Stop passing' in safe_exception(explicit, scrubber, unattended_index_source='dotenv')


def test_unknown_diagnostic_code_cannot_become_visible_guidance():
    from api_agent.testing.diagnostics import safe_diagnostic_code

    identity = TestIdentity('sample::test.py::test_a', 'sample', 'test.py', 'test_a')
    result = TestResult(identity, 'failed', diagnostic_code='narrative-sentinel')

    assert safe_diagnostic_code(result.diagnostic_code) is None
    details = '\n'.join(result_diagnostics([result]))
    assert 'narrative-sentinel' not in details
    assert identity.id in details
