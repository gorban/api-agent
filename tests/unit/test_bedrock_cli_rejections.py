"""Offline native failures retain fixed categories, never diagnostic values."""
import os
import sys

import pytest

from api_agent.llm_providers.bedrock import login_worker as worker
from api_agent.llm_providers.bedrock.login_transport import acquisition_error
from api_agent.providers.contracts import ContractError


CASES = [
    ('Failed to decode the verification code.', 'invalid_request', 'invalid_code'),
    ('Failed to retrieve an auth_code or state from the verification code', 'invalid_request', 'invalid_code'),
    ('Error loading or redeeming a login authorization code: State parameter secret-state does not match expected value expected-state. ', 'authentication', 'state_mismatch'),
    ("Error loading or redeeming a login authorization code: Unable to complete the login process due to an expired authorization code. Please reauthenticate using 'aws login'. ", 'authentication', 'expired_code'),
    ('SSL validation failed for https://synthetic.invalid secret-detail', 'unavailable', 'tls'),
    ('Could not connect to the endpoint URL: "https://synthetic.invalid"', 'unavailable', 'connectivity'),
    ('Connection was closed before we received a valid response from endpoint URL: "https://synthetic.invalid".', 'unavailable', 'connectivity'),
    ('Read timeout on endpoint URL: "https://synthetic.invalid"', 'unavailable', 'connectivity'),
    ('Connect timeout on endpoint URL: "https://synthetic.invalid"', 'unavailable', 'connectivity'),
    ('Failed to connect to proxy URL: "https://synthetic.invalid"', 'unavailable', 'connectivity'),
    ("Unable to create or refresh login credentials due to insufficient permissions. You may be missing permission for the 'signin:CreateOAuth2Token' action.", 'authorization', 'permissions'),
    ('An error occurred (AccessDeniedException): secret-service-detail', 'authorization', 'permissions'),
    ('An error occurred (AccessDenied) when calling the CreateOAuth2Token operation: secret-service-detail', 'authorization', 'permissions'),
    ('An error occurred (UnauthorizedException) when calling the CreateOAuth2Token operation (reached max retries: 4): secret-service-detail', 'authorization', 'permissions'),
]


@pytest.mark.parametrize('text,kind,reason', CASES)
@pytest.mark.parametrize('writer', ['legacy', 'prefixed'])
def test_complete_native_failure_has_fixed_classification(text, kind, reason, writer):
    rendered = text if writer == 'legacy' else 'aws: [ERROR]: ' + text.strip()
    failure = worker.cli_failure(('ordinary prompt\n' + rendered + '\n').encode())
    assert (failure.kind, failure.reason) == (kind, reason)
    assert vars(failure) == {'kind': kind, 'reason': reason}
    assert failure.args == ('Native command failed',)

    def fail():
        raise failure

    with pytest.raises(worker.AcquisitionFailure) as caught:
        worker.diagnosed('sign_in', fail)
    error = acquisition_error(dict(error='acquisition', stage='sign_in', kind=caught.value.kind, reason=caught.value.reason))
    assert error.code == reason
    assert error.execution == 'not_started'
    assert 'secret-' not in str(error)
    assert 'synthetic.invalid' not in str(error)


@pytest.mark.parametrize('output', [
    b'', b'unknown secret-native-detail\xff',
    b'prefix Failed to decode the verification code.',
    b'Failed to decode the verification code. suffix',
    b'An error occurred (OtherException): secret-service-detail',
    b'other: [ERROR]: Failed to decode the verification code.',
    b'aws: [ERROR]:prefix Failed to decode the verification code.',
    b'Error loading or redeeming a login authorization code: State parameter supplied.',
    b'Error loading or redeeming a login authorization code: State parameter supplied',
    b'Error loading or redeeming a login authorization code: State parameter  does not match expected value expected.',
    b'Error loading or redeeming a login authorization code: State parameter supplied does not match expected value .',
    b'An error occurred (AccessDenied) when calling the OtherOperation operation: secret-detail',
    b'Failed to decode the verification code.\nSSL validation failed for endpoint detail',
])
def test_unknown_partial_or_conflicting_diagnostics_remain_unknown(output):
    error = worker.cli_failure(output)
    assert (error.kind, error.reason, error.args) == ('unknown', 'unknown', ('Native command failed',))


def test_multiple_signatures_for_one_category_are_unambiguous():
    error = worker.cli_failure((CASES[0][0] + '\n' + CASES[1][0]).encode())
    assert (error.kind, error.reason) == ('invalid_request', 'invalid_code')


@pytest.mark.parametrize('kind,reason', [('secret-canary', 'invalid_code'), ('authentication', 'secret-canary')])
def test_native_failure_constructor_does_not_retain_unallowlisted_values(kind, reason):
    error = worker.CLIExecutionFailure(kind, reason)
    assert vars(error) == {'kind': 'unknown', 'reason': 'unknown'}
    assert error.args == ('Native command failed',)


@pytest.mark.parametrize('kind,reason', [('invalid_request', 'invalid_code'), ('authentication', 'state_mismatch'), ('authentication', 'expired_code'), ('authorization', 'permissions')])
def test_new_reason_kind_pairs_are_strict(kind, reason):
    with pytest.raises(ContractError):
        acquisition_error(dict(error='acquisition', stage='sign_in', kind='unknown', reason=reason))
    error = acquisition_error(dict(error='acquisition', stage='sign_in', kind=kind, reason=reason))
    assert error.code == reason
    assert len(str(error)) > len(f'AWS sign-in failed. Stage: sign_in; category: {kind}; reason: {reason}.')


@pytest.mark.parametrize('status', [0, 7])
def test_actual_synthetic_child_only_classifies_nonzero_exit(monkeypatch, status):
    # Python child only; no native CLI or user configuration is consulted.
    text = CASES[0][0]
    frames = []
    monkeypatch.setattr(worker, 'emit', frames.append)
    command = [sys.executable, '-I', '-c', f'import sys; sys.stdout.write({text!r}); sys.exit({status})']
    environment = {'PATH': os.defpath}
    if status:
        with pytest.raises(worker.CLIExecutionFailure) as caught:
            worker.cli(command, environment)
        assert (caught.value.kind, caught.value.reason) == ('invalid_request', 'invalid_code')
        assert text not in str(caught.value)
    else:
        assert worker.cli(command, environment) == text.encode()
    assert frames == []


def test_chunked_native_failure_is_classified_after_complete_output(monkeypatch):
    from types import SimpleNamespace
    import io

    chunks = iter([b'Failed to decode ', b'the verification code.', b''])
    class Output(io.BytesIO):
        def fileno(self): return 10
    output = Output()
    process = SimpleNamespace(stdout=output, stdin=None, wait=lambda: 9, poll=lambda: 9)
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(worker.os, 'read', lambda *args: next(chunks))
    class Selector:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def register(self, *args): pass
        def select(self): return [(SimpleNamespace(data='output'), None)]
    monkeypatch.setattr(worker.selectors, 'DefaultSelector', Selector)
    with pytest.raises(worker.CLIExecutionFailure) as caught:
        worker.cli(['synthetic'], {})
    assert caught.value.reason == 'invalid_code'
    assert output.closed


def test_maximum_bounded_state_diagnostic_has_no_backtracking():
    import subprocess
    from pathlib import Path
    script = """from api_agent.llm_providers.bedrock import login_worker as worker
prefix = b'aws: [ERROR]: Error loading or redeeming a login authorization code: State parameter '
repeated = b'x does not match expected value '
body = (repeated * (worker.MAX_OUTPUT // len(repeated)))[:worker.MAX_OUTPUT-len(prefix)-2]
for ending, reason in [(b'!', 'unknown'), (b'.', 'state_mismatch')]:
    error = worker.cli_failure(prefix + body + ending)
    assert error.reason == reason
"""
    subprocess.run([sys.executable, '-c', script], check=True, timeout=2,
                   env={'PATH': os.defpath, 'PYTHONPATH': str(Path(worker.__file__).parents[3])})
