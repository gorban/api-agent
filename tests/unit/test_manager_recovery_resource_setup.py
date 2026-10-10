"""Resource allocation failure closes owned PTYs without spawning a CLI."""
import os
from unittest.mock import Mock

import pytest

from api_agent.auth import recovery
from api_agent.auth.credentials import CredentialRecoveryError


def test_selector_allocation_failure_closes_both_terminal_descriptors(monkeypatch):
    master, slave = os.openpty()
    monkeypatch.setattr(recovery.os, 'openpty', lambda: (master, slave))
    monkeypatch.setattr(recovery.selectors, 'DefaultSelector', Mock(side_effect=OSError('resource exhaustion')))
    channel = Mock()
    interaction = Mock()
    interaction.open.return_value = channel
    try:
        with pytest.raises(CredentialRecoveryError, match='interaction disconnected'):
            recovery.recover(['/unused/cli'], interaction, 'Keeper', 1)
        channel.close.assert_called_once()
        for descriptor in (master, slave):
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in (master, slave):
            try:
                os.close(descriptor)
            except OSError:
                pass
