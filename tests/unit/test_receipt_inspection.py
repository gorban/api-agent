"""Consumer retries preserve every acknowledged cleanup duty."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.control import CleanupPendingError
from api_agent.testing.inspection_controls import (
    InspectionGroup, ReceiptInspection, preserve_cleanup_receipts,
)


def receipt(retry=None):
    return CleanupPendingError((), retry if retry is not None else AsyncMock())


def test_replaced_attachment_keeps_original_and_new_recording_duties():
    original, later = receipt(), receipt()
    primary = KeyboardInterrupt()
    primary.cleanup_pending = original
    preserve_cleanup_receipts(primary)
    adapter = primary.inspections[0]
    group = InspectionGroup(primary.inspections)
    primary.cleanup_pending = group.cleanup_receipt()
    primary.recording_prerequisites = SimpleNamespace(pending_cleanup=later)
    assert group.close(force=True)
    original.retry.assert_awaited_once_with()
    later.retry.assert_awaited_once_with()
    assert adapter.closed and not group.active
    assert group.close(force=True)
    original.retry.assert_awaited_once()


def test_direct_cyclic_receipts_are_retried_once_and_adapter_is_preserved_once():
    first, second = receipt(), receipt()
    first.cleanup_pending = second
    second.cleanup_pending = first
    preserve_cleanup_receipts(first)
    adapter = first.inspections[0]
    preserve_cleanup_receipts(first)
    assert first.inspections == (adapter,)
    assert adapter.end_inspection()
    first.retry.assert_awaited_once_with()
    second.retry.assert_awaited_once_with()
    asyncio.run(adapter.aclose())
    first.retry.assert_awaited_once()


def test_errors_without_receipts_do_not_add_an_inspection():
    primary = ValueError('ordinary failure')
    primary.inspections = ('existing handle',)
    preserve_cleanup_receipts(primary)
    assert primary.inspections == ('existing handle',)


def test_failed_retry_remains_pending_until_a_successful_consumer_retry():
    pending = receipt(AsyncMock(side_effect=[ValueError('not disposed'), ValueError('still pending'), None]))
    adapter = ReceiptInspection(pending)
    assert not adapter.abort()
    assert adapter.pending and not adapter.closed
    with pytest.raises(CleanupPendingError) as caught:
        adapter.wait_closed()
    assert caught.value is pending
    assert adapter.end_inspection()
    assert adapter.closed and not adapter.pending
    assert pending.retry.await_count == 3


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_retry_interruption_preserves_the_receipt_and_releases_its_guard(interruption):
    failure = interruption('interrupted')
    pending = receipt(AsyncMock(side_effect=[failure, None]))
    adapter = ReceiptInspection(pending)
    with pytest.raises(interruption) as caught:
        adapter.end_inspection()
    assert caught.value is failure
    assert adapter.pending
    assert adapter.end_inspection()
    assert adapter.closed


def test_sync_retry_from_a_running_loop_does_not_claim_completion():
    async def scenario():
        pending = receipt()
        adapter = ReceiptInspection(pending)
        assert not adapter.end_inspection()
        pending.retry.assert_not_awaited()
        assert adapter.pending
        await adapter.aclose()
        assert adapter.closed
    asyncio.run(scenario())


def test_parallel_retry_cannot_acknowledge_the_inflight_duty():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def dispose():
            entered.set()
            await release.wait()
        adapter = ReceiptInspection(receipt(dispose))
        first = asyncio.create_task(adapter.aclose())
        try:
            await entered.wait()
            with pytest.raises(ContractError, match='already running'):
                await adapter.aclose()
            assert adapter.pending
        finally:
            release.set()
            await first
        assert adapter.closed
    asyncio.run(scenario())


def test_successful_wait_acknowledges_cleanup():
    pending = receipt()
    adapter = ReceiptInspection(pending)
    adapter.wait_closed()
    assert adapter.closed
    pending.retry.assert_awaited_once_with()
