"""Receipt retries own each duty once; diagnostic tails hide complete secrets."""
import asyncio
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from test_terminal_failure_diagnostics import terminal_failure_launch

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime import prerequisites, tasks


@pytest.fixture
def pending_receipt(monkeypatch):
    """Capture a command receipt without starting or signalling an OS process."""
    process = SimpleNamespace(pid=123456789, returncode=None,
        stdout=SimpleNamespace(close=lambda: None), stderr=SimpleNamespace(close=lambda: None))
    primary = KeyboardInterrupt('synthetic interruption')
    checks = iter((None, primary))

    def check(*args):
        outcome = next(checks)
        if outcome is not None:
            raise outcome

    class Selector:
        def register(self, *args):
            pass

        def get_map(self):
            return {'pending': True}

        def close(self):
            pass

    monkeypatch.setattr(prerequisites, '_check', check)
    monkeypatch.setattr(prerequisites.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(prerequisites, 'selectors', SimpleNamespace(
        DefaultSelector=Selector, EVENT_READ=prerequisites.selectors.EVENT_READ))

    async def controlled_thread(action, *args, **kwargs):
        return await action(*args, **kwargs)

    monkeypatch.setattr(tasks.asyncio, 'to_thread', controlled_thread)

    def capture(duty=None, acknowledged=False):
        if duty is not None:
            primary.cleanup_pending = duty

        def initial_cleanup(owned):
            if acknowledged:
                owned._api_agent_disposal_complete = True
            raise RuntimeError('synthetic pending cleanup')

        monkeypatch.setattr(prerequisites, 'finalize', initial_cleanup)
        with pytest.raises(KeyboardInterrupt) as caught:
            prerequisites.run_command(('synthetic-tool',), environment={}, directory='/tmp',
                deadline=100, cancelled=lambda: False, now=lambda: 0)
        assert caught.value is primary
        return primary.cleanup_pending

    return process, capture


@pytest.mark.parametrize('cancel_first', [False, True])
def test_overlapping_process_retries_never_repeat_acknowledged_finalization(
        pending_receipt, monkeypatch, cancel_first):
    process, capture = pending_receipt
    receipt = capture()
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def finalize(owned):
            calls.append(owned)
            entered.set()
            await release.wait()
            owned._api_agent_disposal_complete = True

        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        first = asyncio.create_task(receipt.retry())
        await entered.wait()
        second = asyncio.create_task(receipt.retry())
        await asyncio.sleep(0)
        observed_calls = list(calls)
        assert receipt.owners == (process,)
        if cancel_first:
            first.cancel()
            await asyncio.sleep(0)
            assert receipt.owners == (process,)
        release.set()
        if cancel_first:
            with pytest.raises(asyncio.CancelledError) as caught:
                await first
            assert caught.value.cleanup_pending is receipt
        else:
            await first
        await second
        assert observed_calls == [process]
        assert calls == [process]
        assert receipt.owners == ()

    asyncio.run(scenario())


def test_overlapping_independent_duties_settle_once(pending_receipt):
    process, capture = pending_receipt
    owner = object()
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def release_owner():
            calls.append(owner)
            entered.set()
            await release.wait()

        duty = CleanupPendingError((owner,), release_owner)
        receipt = capture(duty, acknowledged=True)
        first = asyncio.create_task(receipt.retry())
        await entered.wait()
        second = asyncio.create_task(receipt.retry())
        await asyncio.sleep(0)
        assert receipt.owners == (owner,)
        observed_calls = list(calls)
        release.set()
        await asyncio.gather(first, second)
        assert observed_calls == [owner]
        assert calls == [owner]
        assert receipt.owners == ()
        assert process._api_agent_disposal_complete

    asyncio.run(scenario())


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('read_size', [127, 65536])
def test_diagnostic_scrubbing_precedes_tail_truncation(tmp_path, monkeypatch, stream, read_size):
    secret = 'synthetic-secret-' + 'q' * 3000 + '-secret-end'
    original_read = prerequisites.os.read
    monkeypatch.setattr(prerequisites.os, 'read', lambda fd, size: original_read(fd, min(size, read_size)))
    helper = tmp_path / 'output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), os.environ["API_KEY"].encode() + b"x" * 16000)\nsys.exit(1)\n')
    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), '1' if stream == 'stdout' else '2'),
            environment={'API_KEY': secret}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)
    details = caught.value.local_details
    assert secret not in details
    assert '-secret-end' not in details
    assert '[REDACTED]' in details
    assert 'x' * 16000 in details


def test_functional_stdout_is_preserved_while_diagnostics_are_scrubbed(tmp_path):
    secret = 'synthetic-functional-secret'
    output = prerequisites.run_command((sys.executable, '-c', 'import os; print(os.environ["API_KEY"])'),
        environment={'API_KEY': secret}, directory=tmp_path,
        deadline=time.monotonic() + 5, cancelled=lambda: False)
    assert output == secret + '\n'


def test_cancelled_queued_retry_retains_receipt_ownership(pending_receipt, monkeypatch):
    process, capture = pending_receipt
    receipt = capture()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def finalize(owned):
            calls.append(owned)
            entered.set()
            await release.wait()
            owned._api_agent_disposal_complete = True

        monkeypatch.setattr(prerequisites, 'finalize', finalize)
        first = asyncio.create_task(receipt.retry())
        await entered.wait()
        queued = asyncio.create_task(receipt.retry())
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await queued
        assert caught.value.cleanup_pending is receipt
        assert receipt.owners == (process,)
        assert calls == [process]
        release.set()
        await first
        await receipt.retry()
        assert calls == [process]
        assert receipt.owners == ()

    asyncio.run(scenario())


def test_long_ordinary_token_scrubs_in_bounded_time_with_url_credentials(tmp_path):
    import subprocess

    script = tmp_path / 'scrub.py'
    script.write_text(
        'from api_agent.testing.diagnostics import CredentialScrubber\n'
        'plain = "x" * (8 * 1024 * 1024)\n'
        'text = plain + " https://synthetic-user:synthetic-password@example.invalid/path"\n'
        'result = CredentialScrubber()(text)\n'
        'assert result.startswith(plain)\n'
        'assert "synthetic-user" not in result and "synthetic-password" not in result\n'
        'assert result.endswith("https://[REDACTED]:[REDACTED]@example.invalid/path")\n')
    environment = {**os.environ, "PYTHONPATH": str(Path(prerequisites.__file__).parents[2])}
    result = subprocess.run((sys.executable, str(script)), capture_output=True, text=True,
                            env=environment, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('cyclic', [False, True])
def test_successor_provenance_survives_aggregate_diagnostics(pending_receipt, cyclic):
    process, capture = pending_receipt
    owner_a, owner_b = object(), object()
    calls = []

    async def settle_b():
        calls.append('b')

    successor = CleanupPendingError((owner_b,), settle_b)

    async def settle_a():
        calls.append('a')
        raise successor

    predecessor = CleanupPendingError((owner_a,), settle_a)
    if cyclic:
        successor.cleanup_pending = predecessor
    receipt = capture(predecessor, acknowledged=True)

    async def scenario():
        with pytest.raises(CleanupPendingError) as caught:
            await receipt.retry()
        assert caught.value.cleanup_pending is receipt
        if cyclic:
            assert set(receipt.owners) == {owner_a, owner_b}
            with pytest.raises(CleanupPendingError):
                await receipt.retry()
            assert receipt.owners == (owner_a,)
            with pytest.raises(CleanupPendingError):
                await receipt.retry()
            assert receipt.owners == (owner_a,)
            assert calls == ['a', 'a', 'b', 'a']
        else:
            assert receipt.owners == (owner_b,)
            await receipt.retry()
            await receipt.retry()
            assert receipt.owners == ()
            assert calls == ['a', 'b']
        assert process._api_agent_disposal_complete

    asyncio.run(scenario())


@pytest.mark.parametrize('scheme', ['https', 'http', 'ssh', 'ftp', 'git+ssh', 'custom.1-foo'])
def test_ansi_reset_is_a_url_boundary(scheme):
    from api_agent.testing.diagnostics import CredentialScrubber

    prefix = '\x1b[0m'
    result = CredentialScrubber()(prefix + scheme + '://synthetic-user:synthetic-password@example.invalid/path')
    assert result == prefix + scheme + '://[REDACTED]:[REDACTED]@example.invalid/path'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_unicode_tail_emits_at_most_16384_bytes(tmp_path, stream):
    helper = tmp_path / 'unicode_output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), ("€" * 5462).encode())\nsys.exit(1)\n')
    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), '1' if stream == 'stdout' else '2'),
            environment={}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)
    sections = caught.value.local_details.split('\n' + stream + ' (', 1)[1]
    header, body = sections.split('\n', 1)
    body = body.split('\nstderr (', 1)[0]
    assert len(body.encode()) == 16383
    assert body == '€' * 5461
    assert header == '16386 bytes captured; 3 bytes omitted):'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_bootstrap_terminal_scrubs_ansi_prefixed_url(
        terminal_failure_launch, tmp_path, monkeypatch, capsys, stream):
    from api_agent.runtime.bootstrap import main
    from test_terminal_failure_diagnostics import ARGS, ROOT

    helper = tmp_path / 'ansi_output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), b"\\x1b[0mhttps://synthetic-user:synthetic-password@example.invalid/path")\nsys.exit(7)\n')

    def fail_integration(*args, **kwargs):
        prerequisites.run_command((sys.executable, str(helper), '1' if stream == 'stdout' else '2'),
            environment={}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', fail_integration)
    assert main(ARGS, base_root=ROOT) == 1
    terminal = capsys.readouterr().err
    assert 'Exit code: 7' in terminal
    assert 'example.invalid/path' in terminal
    assert 'synthetic-user' not in terminal
    assert 'synthetic-password' not in terminal


def test_aggregate_receipt_rethrow_keeps_pending_duty(pending_receipt):
    process, capture = pending_receipt
    owner = object()
    attempts = []

    async def settle_owner():
        attempts.append(owner)
        if len(attempts) == 1:
            raise receipt

    duty = CleanupPendingError((owner,), settle_owner)
    receipt = capture(duty, acknowledged=True)

    async def scenario():
        with pytest.raises(CleanupPendingError) as caught:
            await receipt.retry()
        assert caught.value is receipt
        assert receipt.receipts == (duty,)
        assert receipt.owners == (owner,)
        await receipt.retry()
        assert receipt.owners == ()
        assert attempts == [owner, owner]
        assert process._api_agent_disposal_complete

    asyncio.run(scenario())


@pytest.mark.parametrize('registered, credential_text', [
    ('https', 'https://synthetic-user:synthetic-secondary-secret@example.invalid/path'),
    ('Basic', 'Authorization: Basic synthetic-secondary-secret'),
    ('Bearer', 'Authorization: Bearer synthetic-secondary-secret'),
    ('Bearer', 'Bearer synthetic-secondary-secret'),
])
def test_registered_scheme_masking_preserves_secondary_credential_scrubbing(registered, credential_text):
    from api_agent.testing.diagnostics import CredentialScrubber

    diagnostic = CredentialScrubber({'PASSWORD': registered})(credential_text)
    assert registered not in diagnostic
    assert 'synthetic-secondary-secret' not in diagnostic
    assert 'synthetic-user' not in diagnostic
    assert '[REDACTED]' in diagnostic


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('registered, credential_text', [
    ('https', 'https://synthetic-user:synthetic-secondary-secret@example.invalid/path'),
    ('Basic', 'Authorization: Basic synthetic-secondary-secret'),
    ('Bearer', 'Authorization: Bearer synthetic-secondary-secret'),
    ('Bearer', 'Bearer synthetic-secondary-secret'),
])
def test_bootstrap_terminal_scrubs_secondary_credentials_before_registered_scheme(
        terminal_failure_launch, tmp_path, monkeypatch, capsys, stream, registered, credential_text):
    from api_agent.runtime.bootstrap import main
    from test_terminal_failure_diagnostics import ARGS, ROOT

    helper = tmp_path / 'registered_scheme_output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), os.environ["SYNTHETIC_OUTPUT"].encode())\nsys.exit(7)\n')

    def fail_integration(*args, **kwargs):
        prerequisites.run_command((sys.executable, str(helper), '1' if stream == 'stdout' else '2'),
            environment={'PASSWORD': registered, 'SYNTHETIC_OUTPUT': credential_text}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', fail_integration)
    assert main(ARGS, base_root=ROOT) == 1
    terminal = capsys.readouterr().err
    assert 'Exit code: 7' in terminal
    assert 'synthetic-secondary-secret' not in terminal
    assert 'synthetic-user' not in terminal
    assert '[REDACTED]' in terminal


REGISTERED_SYNTAX = [
    'Authorization: Basic synthetic-inner-credential',
    'Bearer synthetic-inner-credential',
    'password=synthetic-inner-credential',
    'https://synthetic-inner-user:synthetic-inner-password@example.invalid/path',
]


@pytest.mark.parametrize('syntax', REGISTERED_SYNTAX)
def test_complete_registered_value_is_redacted_despite_nested_syntax(syntax):
    from api_agent.testing.diagnostics import CredentialScrubber

    secret = 'synthetic-registered-head ' + syntax + ' synthetic-registered-tail'
    assert CredentialScrubber({'API_KEY': secret})(secret) == '[REDACTED]'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('padding', [0, 16000])
@pytest.mark.parametrize('syntax', REGISTERED_SYNTAX)
def test_bootstrap_terminal_retains_no_composite_registered_secret_fragment(
        terminal_failure_launch, tmp_path, monkeypatch, capsys, stream, padding, syntax):
    from api_agent.runtime.bootstrap import main
    from test_terminal_failure_diagnostics import ARGS, ROOT

    suffix = 'synthetic-registered-tail-sentinel'
    secret = 'synthetic-registered-head-' + 'q' * 3000 + ' ' + syntax + ' ' + suffix
    helper = tmp_path / 'composite_output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), os.environ["API_KEY"].encode() + b"z" * int(sys.argv[2]))\nsys.exit(7)\n')

    def fail_integration(*args, **kwargs):
        prerequisites.run_command((sys.executable, str(helper), '1' if stream == 'stdout' else '2', str(padding)),
            environment={'API_KEY': secret}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)

    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', fail_integration)
    assert main(ARGS, base_root=ROOT) == 1
    terminal = capsys.readouterr().err
    assert 'Exit code: 7' in terminal
    assert '[REDACTED]' in terminal
    assert suffix not in terminal
    assert 'q' * 200 not in terminal


@pytest.mark.parametrize('syntax', REGISTERED_SYNTAX)
def test_functional_stdout_preserves_composite_registered_secret(tmp_path, syntax):
    secret = 'synthetic-registered-head ' + syntax + ' synthetic-registered-tail'
    output = prerequisites.run_command((sys.executable, '-c', 'import os; print(os.environ["API_KEY"])'),
        environment={'API_KEY': secret}, directory=tmp_path,
        deadline=time.monotonic() + 5, cancelled=lambda: False)
    assert output == secret + '\n'


@pytest.mark.parametrize('secrets, text, expected', [
    (('aba',), 'ababa', '[REDACTED]'),
    (('left', 'right'), 'leftright', '[REDACTED]'),
    (('alpha', 'middle', 'omega'),
     'alpha Authorization: Basic inner-one middle password=inner-two omega',
     '[REDACTED] Authorization: Basic [REDACTED] [REDACTED] password=[REDACTED] [REDACTED]'),
    (('inner-one middle password=inner-two',),
     'alpha Authorization: Basic inner-one middle password=inner-two omega',
     'alpha Authorization: Basic [REDACTED] omega'),
])
def test_registered_ranges_keep_all_original_matches_through_multiple_edits(secrets, text, expected):
    from api_agent.testing.diagnostics import CredentialScrubber

    assert CredentialScrubber(secret_values=secrets)(text) == expected


def test_registered_range_expands_with_inner_redaction_without_losing_context():
    from api_agent.testing.diagnostics import CredentialScrubber

    secret = 'synthetic-head Authorization: Basic x synthetic-tail'
    text = 'useful-prefix ' + secret + ' useful-suffix'
    diagnostic = CredentialScrubber({'API_KEY': secret})(text)
    assert diagnostic == 'useful-prefix [REDACTED] useful-suffix'


@pytest.mark.parametrize('secret, text, expected', [
    ('aba', 'ababa', '[REDACTED]'),
    ('ababaca', 'ababababaca', 'abab[REDACTED]'),
    ('aaaaab', 'aaaaaaaaab', 'aaaa[REDACTED]'),
    ('abc', 'abc abc', '[REDACTED] [REDACTED]'),
    ('abc', 'abx ab', 'abx ab'),
    ('x', 'xxxx', '[REDACTED]'),
])
def test_exact_overlapping_secret_matches_preserve_unmatched_context(secret, text, expected):
    from api_agent.testing.diagnostics import CredentialScrubber

    assert CredentialScrubber(secret_values=(secret,))(text) == expected


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_dense_overlapping_registered_output_has_bounded_diagnostics_and_owned_cleanup(tmp_path, stream):
    import subprocess

    helper = tmp_path / 'dense_output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), b"A" * (8 * 1024 * 1024))\nsys.exit(7)\n')
    driver = tmp_path / 'dense_diagnostics.py'
    driver.write_text(
        'import sys, time\n'
        'from api_agent.runtime.prerequisites import run_command, PrerequisiteError\n'
        'from api_agent.testing.processes import disposal_complete\n'
        'import api_agent.runtime.prerequisites as prerequisites\n'
        'owned = []\n'
        'original_finalize = prerequisites.finalize\n'
        'def finalize(process):\n'
        '    owned.append(process)\n'
        '    return original_finalize(process)\n'
        'prerequisites.finalize = finalize\n'
        'try:\n'
        '    run_command((sys.executable, sys.argv[1], sys.argv[2]), environment={"API_KEY": "A" * 65536},\n'
        '                directory=sys.argv[3], deadline=time.monotonic() + 5, cancelled=lambda: False)\n'
        'except PrerequisiteError as error:\n'
        '    assert "A" * 200 not in error.local_details\n'
        '    assert "[REDACTED]" in error.local_details and "Exit code: 7" in error.local_details\n'
        '    assert len(error.local_details) < 1000\n'
        'else:\n'
        '    raise AssertionError("owned helper must fail")\n'
        'assert len(owned) == 1\n'
        'assert disposal_complete(owned[0]) and owned[0].returncode == 7\n')
    environment = {**os.environ, 'PYTHONPATH': str(Path(prerequisites.__file__).parents[2])}
    result = subprocess.run((sys.executable, str(driver), str(helper), '1' if stream == 'stdout' else '2', str(tmp_path)),
        env=environment, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_dense_secret_occurrences_emit_one_merged_range():
    from api_agent.testing.diagnostics import _overlapping_ranges

    ranges = list(_overlapping_ranges('A' * 100000, 'A' * 50000))
    assert ranges == [(0, 100000)]
