"""Credential-only terminal diagnostics preserve useful captured failure evidence."""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.config.resolve import ConfigResult
from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.diagnostics import CredentialScrubber, format_exception, result_diagnostics, terminal_exception
from api_agent.testing.inspection_controls import finish_terminal_inspection


@pytest.mark.parametrize('source,expected', [
    ('API_KEY=synthetic-secret reason: server rejected /tmp/config.py:19', 'API_KEY=[REDACTED] reason: server rejected /tmp/config.py:19'),
    ('{"password": "synthetic-secret", "reason": "port in use"}', '{"password": "[REDACTED]", "reason": "port in use"}'),
    ('Authorization: Basic synthetic-secret failed at /tmp/client.py', 'Authorization: Basic [REDACTED] failed at /tmp/client.py'),
    ('Authorization: Bearer synthetic-secret failed at /tmp/client.py', 'Authorization: Bearer [REDACTED] failed at /tmp/client.py'),
    ('https://alice:synthetic-secret@example.test:443/resource', 'https://[REDACTED]:[REDACTED]@example.test:443/resource'),
    ('Cookie: session=synthetic-secret; second=other-secret; reason remains', 'Cookie: session=[REDACTED]; second=[REDACTED]; reason remains'),
    ('runner --password "synthetic-secret" --root /tmp/suite', 'runner --password "[REDACTED]" --root /tmp/suite'),
    ('TOKEN_USAGE_CSV=/tmp/usage.csv max_tokens=2000 password=None PasswordError: missing setting', 'TOKEN_USAGE_CSV=/tmp/usage.csv max_tokens=2000 password=None PasswordError: missing setting'),
])
def test_recognizable_credentials_scrub_values_preserving_reasons_paths_and_categories(source, expected):
    assert CredentialScrubber()(source) == expected


def test_registered_and_nested_config_credentials_are_scrubbed_with_content_disabled():
    boundary = EmissionBoundary(ContentPolicy(False), secrets=('registered-runtime-secret',))
    config = ConfigResult({'SYNTHETIC_API_KEY': 'configured-secret', 'credentials': {'password': ['nested-secret']},
        'TOKEN_USAGE_CSV': '/tmp/usage.csv', 'LOG_CONTENT': False}, {}, (), (), ())
    scrubber = CredentialScrubber(config, secret_values=('explicit-secret',), boundary=boundary)
    source = 'configured-secret nested-secret registered-runtime-secret explicit-secret /tmp/usage.csv cause remains'
    assert scrubber(source) == '[REDACTED] [REDACTED] [REDACTED] [REDACTED] /tmp/usage.csv cause remains'


def result(identifier, status, trace='', local=''):
    return SimpleNamespace(identity=SimpleNamespace(id=identifier), status=status,
        traceback=trace, local_diagnostic=local)


def test_result_details_prefer_local_and_identify_each_failure_once():
    rows = [result('first', 'passed'), result('first', 'error', 'original pytest', 'local command cause'),
        result('first', 'error', 'duplicate'), result('second', 'failed', 'original second trace'),
        result('third', 'passed', local='cleanup cause'), result('fourth', 'error')]
    details = list(result_diagnostics(rows))
    assert len(details) == 4
    assert details[0] == 'Failure details: first\nlocal command cause'
    assert details[1] == 'Failure details: second\noriginal second trace'
    assert details[2] == 'Failure details: third\ncleanup cause'
    assert details[3].endswith('No failure diagnostic was captured for this test.')


def test_terminal_completion_emits_only_through_explicit_diagnostic_channel():
    rows = [result('test-one', 'failed', 'safe trace', 'command /tmp/job exit 1 stdout reason stderr API_KEY=synthetic-secret')]
    recorded = SimpleNamespace(execution=SimpleNamespace(tests=rows, cancelled=False, inspections=()), persisted=True)
    summary, details = [], []
    scrubber = CredentialScrubber()
    assert finish_terminal_inspection(recorded, summary.append, diagnostic_output=lambda text: details.append(scrubber(text)))
    assert details == ['Failure details: test-one\ncommand /tmp/job exit 1 stdout reason stderr API_KEY=[REDACTED]']
    assert all('API_KEY' not in line for line in summary)
    details.clear()
    assert finish_terminal_inspection(recorded, summary.append)
    assert details == []


def test_wrapper_preserves_cleanup_receipt_identity_and_retry():
    owner = object()
    retries = []
    async def retry():
        retries.append(owner)
    receipt = CleanupPendingError((owner,), retry)
    receipt.local_details = 'command exit 1 stderr secret=synthetic-secret reason retained'
    group, recorded = object(), object()
    receipt.inspection_group = group
    receipt.inspections = (owner,)
    receipt.recorded_result = recorded
    receipt.recording_prerequisites = owner
    wrapped = terminal_exception(receipt, CredentialScrubber())
    assert isinstance(wrapped, RuntimeError)
    assert wrapped.cleanup_pending is receipt
    assert wrapped.owners is receipt.owners and wrapped.retry is receipt.retry
    assert wrapped.inspection_group is group and wrapped.inspections is receipt.inspections
    assert wrapped.recorded_result is recorded and wrapped.recording_prerequisites is owner
    asyncio.run(wrapped.retry())
    assert retries == [owner]
    assert 'synthetic-secret' not in str(wrapped) and 'reason retained' in str(wrapped)
    assert 'synthetic-secret' in receipt.local_details


@pytest.mark.parametrize('error,expected_type,code', [(KeyboardInterrupt('secret=synthetic-secret'), KeyboardInterrupt, None),
    (SystemExit(130), SystemExit, 130), (SystemExit('secret=synthetic-secret'), SystemExit, 1), (SystemExit(), SystemExit, None)])
def test_wrapper_preserves_control_flow(error, expected_type, code):
    wrapped = terminal_exception(error, CredentialScrubber())
    assert isinstance(wrapped, expected_type)
    if isinstance(wrapped, SystemExit):
        assert wrapped.code == code
        assert 'synthetic-secret' not in wrapped.diagnostic
    else:
        assert 'synthetic-secret' not in str(wrapped)


def test_unhandled_wrapper_does_not_expose_original_source_literal(tmp_path):
    secret = 'synthetic-credential-literal'
    script = tmp_path / 'wrapped_failure.py'
    script.write_text('from api_agent.testing.diagnostics import CredentialScrubber, terminal_exception\n'
        f'scrubber = CredentialScrubber({{"API_KEY": {secret!r}}})\n'
        'try:\n'
        f'    raise RuntimeError({secret!r})\n'
        'except RuntimeError as error:\n'
        '    error.local_details = "command exit 1 stdout meaningful cause stderr unavailable"\n'
        '    raise terminal_exception(error, scrubber) from None\n')
    run = subprocess.run([sys.executable, str(script)], text=True, capture_output=True)
    assert run.returncode == 1
    assert secret not in run.stdout + run.stderr
    assert 'RuntimeError: [REDACTED]' in run.stderr
    assert 'meaningful cause' in run.stderr and str(script) in run.stderr


def test_descriptor_metadata_remains_useful_while_nested_credentials_are_scrubbed():
    config = {'auth': {'credentials': {'manager': 'keeper', 'cli_path': '/bin/keeper',
        'password': 'nested-secret', 'token': 'nested-token'}}, 'LLM_CREDENTIAL_ROLE': 'LLM User'}
    source = 'api_agent/auth/keeper.py /bin/keeper executable-not-found LLM User nested-secret nested-token'
    assert CredentialScrubber(config)(source) == 'api_agent/auth/keeper.py /bin/keeper executable-not-found LLM User [REDACTED] [REDACTED]'


@pytest.mark.parametrize('source,expected', [
    ('https://opaque-token@example.test/path', 'https://[REDACTED]@example.test/path'),
    ('Authorization: Custom opaque-token\nreason remains', 'Authorization: Custom [REDACTED]\nreason remains'),
    ('RuntimeError: secret=opaque-token reason remains', 'RuntimeError: secret=[REDACTED] reason remains'),
    ('input_tokens=12 output_tokens=8 total_tokens=20 reason remains', 'input_tokens=12 output_tokens=8 total_tokens=20 reason remains'),
])
def test_opaque_authentication_and_inner_assignments_are_minimally_scrubbed(source, expected):
    scrubber = CredentialScrubber()
    assert scrubber(source) == expected
    assert scrubber(expected) == expected


def test_known_multiline_credentials_and_escaped_forms_are_safe_for_line_buffering():
    boundary = EmissionBoundary(ContentPolicy(False), secrets=('registered-line-A\nregistered-line-B',))
    scrubber = CredentialScrubber({'API_KEY': 'configured-line-A\nconfigured-line-B'}, boundary=boundary)
    lines = ['stdout configured-line-A\n', 'configured-line-B reason retained\n',
        'registered-line-A\n', 'registered-line-B\n', 'configured-line-A\\nconfigured-line-B']
    text = ''.join(scrubber(line) for line in lines)
    for fragment in ('configured-line-A', 'configured-line-B', 'registered-line-A', 'registered-line-B'):
        assert fragment not in text
    assert 'stdout' in text and 'reason retained' in text


def test_dotted_credential_assignments_preserve_source_and_public_accounting():
    source = "self.password='unregistered' settings.client_secret=other-secret settings.TOKEN_USAGE_CSV=/tmp/usage.csv settings.input_tokens=12 reason /tmp/client.py:8"
    expected = "self.password='[REDACTED]' settings.client_secret=[REDACTED] settings.TOKEN_USAGE_CSV=/tmp/usage.csv settings.input_tokens=12 reason /tmp/client.py:8"
    assert CredentialScrubber()(source) == expected


def test_structured_auth_descriptor_text_preserves_containers_and_operational_fields():
    config = {'credentials': {'manager': 'keeper', 'password': 'actual-secret'}}
    source = "credentials={'manager': 'keeper', 'password': 'actual-secret'} /bin/keeper executable-not-found"
    expected = "credentials={'manager': 'keeper', 'password': '[REDACTED]'} /bin/keeper executable-not-found"
    assert CredentialScrubber(config)(source) == expected


def test_registration_ignores_empty_fragments_and_non_string_metadata_keys():
    config = {'API_KEY': '\n\nactual-secret', 'credentials': {object(): 'public metadata', 'password': ''}}
    scrubber = CredentialScrubber(config)
    assert scrubber('actual-secret public metadata') == '[REDACTED] public metadata'


def test_explicit_terminal_diagnostic_precedes_final_summary_and_publication():
    rows = [result('test-one', 'error', local='full command exit/stdout/stderr cause')]
    recorded = SimpleNamespace(execution=SimpleNamespace(tests=rows, cancelled=False, inspections=()), persisted=True)
    ordered = []
    finish_terminal_inspection(recorded, ordered.append, diagnostic_output=ordered.append)
    assert ordered[0] == 'Failure details: test-one\nfull command exit/stdout/stderr cause'
    assert ordered[1].startswith('Integration results: FAIL')
    assert ordered[2] == 'Allure publication: PASS'


@pytest.mark.parametrize('source,expected', [
    ("password=['unregistered-first', 'unregistered-second'] reason remains", "password=[REDACTED] reason remains"),
    ('api_key=["unregistered-first", ["nested-second", "quoted]bracket"]] /tmp/source.py', 'api_key=[REDACTED] /tmp/source.py'),
    ('--password=opaque --api-key=second-opaque --root=/tmp/source', '--password=[REDACTED] --api-key=[REDACTED] --root=/tmp/source'),
    ("password=['partial-secret' reason remains", "password=['[REDACTED]' reason remains"),
    ("password=['partial-secret' reason remains\nRuntimeError: next reason", "password=['[REDACTED]' reason remains\nRuntimeError: next reason"),
    ("password=['escaped\\'quote', 'second'] reason", "password=[REDACTED] reason"),
])
def test_sensitive_lists_and_equals_options_preserve_reasons(source, expected):
    assert CredentialScrubber()(source) == expected


def grouped_error():
    def suppressed_child():
        try:
            raise KeyError('synthetic suppressed child cause')
        except KeyError:
            try:
                raise ValueError('synthetic sixteenth actual cause') from None
            except ValueError as error:
                error.local_details = 'child command exit 1 stdout cause stderr specific cause'
                return error
    child = suppressed_child()
    group = ExceptionGroup('synthetic sixteen failures',
        [RuntimeError(f'synthetic child {number}') for number in range(15)] + [child])
    for number in range(12):
        group = ExceptionGroup(f'synthetic nested group {number}', [group])
    return group


def test_all_exception_group_children_and_suppressed_child_evidence_are_formatted():
    text = format_exception(grouped_error())
    assert 'synthetic sixteenth actual cause' in text
    assert 'synthetic suppressed child cause' in text
    assert 'child command exit 1 stdout cause stderr specific cause' in text
    assert 'max_group_depth' not in text and 'and 1 more exception' not in text


def test_shell_secret_registration_preserves_unrelated_arguments_across_randomized_quotes():
    import random
    import shlex
    rng = random.Random(42)
    for _ in range(40):
        secret = 'synthetic' + ''.join(rng.choice("ab'\" ") for _ in range(16))
        scrubber = CredentialScrubber(secret_values=(secret,))
        command = shlex.join(('tool', '--opaque', secret, '--root', '/tmp/noncredential'))
        assert scrubber(command) == 'tool --opaque [REDACTED] --root /tmp/noncredential'


def test_unregistered_shell_credential_option_scrubs_concatenated_quoted_argument():
    import shlex
    command = shlex.join(('tool', '--password', "synthetic'credential", '--root', '/tmp/suite'))
    assert CredentialScrubber()(command) == "tool --password '[REDACTED]' --root /tmp/suite"


def test_truncated_noncredential_shell_argument_is_retained():
    text = "Command: tool --root 'public long path [truncated: 42 command characters omitted]\nExit code: 1"
    assert CredentialScrubber()(text) == text


@pytest.mark.parametrize('secret', ['head,tail', 'head\ntail', "head'tail", 'head tail'])
def test_structured_command_scrubs_full_credential_arguments_before_rendering(secret):
    from api_agent.testing.diagnostics import scrub_command
    assert scrub_command(('tool', '--password', secret, '--api-key=' + secret, '--root', '/tmp/noncredential')) == (
        'tool', '--password', '[REDACTED]', '--api-key=[REDACTED]', '--root', '/tmp/noncredential')


def test_structured_command_preserves_noncredential_values_and_scrubs_registered_values():
    from api_agent.testing.diagnostics import scrub_command
    assert scrub_command(('tool', '--opaque', 'registered-secret', '--root=/tmp/public,local', 'public\ncontext'),
        {'API_KEY': 'registered-secret'}) == ('tool', '--opaque', '[REDACTED]', '--root=/tmp/public,local', 'public\ncontext')


@pytest.mark.parametrize('secret', ['head,tail', 'head\ntail', "head'tail", 'head tail', 'head,' + 'x' * 5000, ''])
@pytest.mark.parametrize('key', ['PASSWORD', 'api_key', 'client-secret'])
def test_raw_credential_assignments_scrub_complete_values(secret, key):
    from api_agent.testing.diagnostics import scrub_command

    scrubbed = scrub_command(('tool', key + '=' + secret, 'ROOT=/tmp/public,local', 'MODE=public\ncontext'))

    assert scrubbed == ('tool', key + '=[REDACTED]', 'ROOT=/tmp/public,local', 'MODE=public\ncontext')
