"""Managed log capture masks complete streams before presentation clipping."""
import os
import sys
import time

import pytest

from api_agent.runtime.prerequisites import PrerequisiteError, run_command
from api_agent.testing.diagnostics import CredentialScrubber


SECRET = 'synthetic-log-credential-0123456789'


def execute(tmp_path, script, **options):
    return run_command((sys.executable, '-c', script), environment={'PATH': os.environ['PATH']},
                       directory=tmp_path, deadline=time.monotonic() + 5,
                       cancelled=lambda: False, **options)


def test_default_result_preserves_the_existing_stdout_contract(tmp_path):
    output = execute(tmp_path, 'import os; os.write(1,b"ordinary output"); os.write(2,b"separate warning")')

    assert output == 'ordinary output'


def test_combined_result_labels_both_streams_and_masks_credentials(tmp_path):
    output = execute(tmp_path,
        'import os; os.write(1,b"ordinary output"); os.write(2,b"Bearer secondary-credential")',
        combine_output=True)

    assert output.startswith('stdout:\nordinary output\n\nstderr:\n')
    assert 'secondary-credential' not in output
    assert '[REDACTED]' in output


def test_configured_secret_is_masked_before_any_output_is_clipped(tmp_path):
    script = ('import os; assert "API_KEY" not in os.environ; '
              f'os.write(1, b"x" * 65530 + {SECRET[:12].encode()!r}); '
              f'os.write(1, {SECRET[12:].encode()!r}); os.write(2, {SECRET.encode()!r})')

    output = execute(tmp_path, script, combine_output=True,
                     output_scrubber=CredentialScrubber({'API_KEY': SECRET}))

    assert 'x' * 65530 in output
    assert SECRET not in output
    assert SECRET[:12] not in output
    assert SECRET[12:] not in output
    assert output.count('[REDACTED]') == 2


def test_error_evidence_uses_the_same_configured_secret_boundary(tmp_path):
    script = f'import os; os.write(2, {SECRET.encode()!r}); raise SystemExit(2)'

    with pytest.raises(PrerequisiteError) as failure:
        execute(tmp_path, script, combine_output=True,
                output_scrubber=CredentialScrubber({'API_KEY': SECRET}))

    assert SECRET not in failure.value.local_details
    assert '[REDACTED]' in failure.value.local_details
    assert 'Exit code: 2' in failure.value.local_details


@pytest.mark.parametrize('completed_disposal', [False, True])
@pytest.mark.parametrize('exit_code', [0, 2])
def test_ordinary_cleanup_failure_exports_one_retry_owner(tmp_path, monkeypatch, completed_disposal, exit_code):
    import asyncio
    import api_agent.runtime.prerequisites as prerequisites

    original = prerequisites.finalize
    finalized = []
    def fail_once(process):
        finalized.append(process)
        if len(finalized) == 1:
            if completed_disposal:
                original(process)
            raise RuntimeError('synthetic cleanup failure')
        original(process)
    monkeypatch.setattr(prerequisites, 'finalize', fail_once)

    with pytest.raises(PrerequisiteError, match='cleanup failed') as failure:
        execute(tmp_path, f'raise SystemExit({exit_code})', combine_output=True)

    receipt = failure.value.cleanup_pending
    process = finalized[0]
    assert receipt.owners == (() if completed_disposal else (process,))

    async def retry_twice():
        await asyncio.gather(receipt.retry(), receipt.retry())
    asyncio.run(retry_twice())

    assert receipt.owners == ()
    assert prerequisites.disposal_complete(process)
    assert len(finalized) == (1 if completed_disposal else 2)


@pytest.mark.parametrize('limit', [256, 65536])
def test_combined_byte_limit_retains_both_stream_origins_after_scrubbing(tmp_path, limit):
    script = ('import os; '
              f'os.write(1, b"a" * 70000 + {SECRET.encode()!r} + b"OUT-END"); '
              f'os.write(2, b"b" * 70000 + {SECRET.encode()!r} + b"ERR-END")')

    output = execute(tmp_path, script, combine_output=True, output_limit=limit,
                     output_scrubber=CredentialScrubber({'API_KEY': SECRET}))

    assert len(output.encode('utf-8')) <= limit
    stdout, stderr = output.split('\n\nstderr:\n')
    assert stdout.startswith('stdout:\n[Earlier output omitted]\n')
    assert stderr.startswith('[Earlier output omitted]\n')
    assert stdout.endswith('[REDACTED]OUT-END') and stderr.endswith('[REDACTED]ERR-END')
    assert SECRET not in output


@pytest.mark.parametrize('limit', [True, '256', 255])
def test_invalid_output_bound_is_rejected_before_launch(tmp_path, monkeypatch, limit):
    import api_agent.runtime.prerequisites as prerequisites
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid limit must not start a process')
    monkeypatch.setattr(prerequisites.subprocess, 'Popen', forbidden)

    with pytest.raises(ValueError, match='byte limit'):
        execute(tmp_path, 'pass', combine_output=True, output_limit=limit)


def test_small_combined_output_keeps_exact_bytes_within_limit(tmp_path):
    assert execute(tmp_path, 'print("ordinary")', combine_output=True, output_limit=256) == 'stdout:\nordinary\n\n\nstderr:\n'


@pytest.mark.parametrize('stream', [1, 2])
def test_registered_secret_spanning_the_raw_tail_is_scrubbed_before_clipping(tmp_path, stream):
    secret = 'synthetic-owned-start-' + 'q' * 300 + '-sensitive-owned-suffix'
    script = f'import os; os.write({stream}, {secret.encode()!r})'

    output = execute(tmp_path, script, combine_output=True, output_limit=256,
                     output_scrubber=CredentialScrubber(secret_values=(secret,)))

    assert '-sensitive-owned-suffix' not in output
    assert '[REDACTED]' in output


def test_registered_argv_secret_is_scrubbed_inside_a_larger_quoted_argument(tmp_path):
    secret = "synthetic owned ' quote " + 'q' * 5000 + '-sensitive-owned-suffix'
    argument = 'ordinary-prefix-' + secret + '-ordinary-suffix'

    with pytest.raises(PrerequisiteError) as failure:
        run_command((sys.executable, '-c', 'raise SystemExit(2)', argument),
            environment={'PATH': os.environ['PATH']}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False,
            output_scrubber=CredentialScrubber(secret_values=(secret,)))

    assert 'synthetic owned' not in failure.value.local_details
    assert '-sensitive-owned-suffix' not in failure.value.local_details
    assert '[REDACTED]' in failure.value.local_details
