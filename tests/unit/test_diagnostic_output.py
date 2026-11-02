"""Diagnostic capture is scoped to its operation, including offloaded work."""
import asyncio

from api_agent.providers.diagnostic_output import bounded_stderr, bounded_stdout, diagnostic_output, observe_stderr


def test_overlapping_operations_and_offload_keep_distinct_stderr():
    first, second = [], []

    async def work(sink, text):
        with diagnostic_output(sink):
            await asyncio.sleep(0)
            await asyncio.to_thread(observe_stderr, text)

    async def run():
        await asyncio.gather(work(first.append, 'first'), work(second.append, 'second'))
        observe_stderr('outside')

    asyncio.run(run())
    assert first == ['first'] and second == ['second']


def test_exception_restores_outer_capture():
    outer, inner = [], []
    with diagnostic_output(outer.append):
        try:
            with diagnostic_output(inner.append):
                observe_stderr('inner')
                raise ValueError('synthetic')
        except ValueError:
            observe_stderr('outer')
    observe_stderr('outside')
    assert outer == ['outer'] and inner == ['inner']


def test_oversized_stderr_is_withheld_without_splitting_credentials():
    assert bounded_stderr('ordinary stderr') == 'ordinary stderr'
    text = 'synthetic-credential-' * 4000
    retained = bounded_stderr(text)
    assert 'stderr omitted' in retained
    assert 'synthetic' not in retained and 'credential' not in retained
    assert bounded_stdout('ordinary stdout') == 'ordinary stdout'
    assert 'stdout omitted' in bounded_stdout(text)
