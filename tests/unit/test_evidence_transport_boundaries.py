"""Evidence transport rejects corrupt or resource-exhausting input."""
import base64
import hashlib

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.setup_transport import _EvidenceReceiver, _bounded_evidence


def _start(body):
    return {'version': 1, 'status': 'evidence_start', 'phase': 'request',
            'reason': 'captured', 'length': len(body), 'sha256': hashlib.sha256(body).hexdigest()}


def _part(body):
    return {'version': 1, 'status': 'evidence_part', 'phase': 'request',
            'offset': 0, 'data': base64.b64encode(body).decode('ascii')}


@pytest.mark.parametrize('body', [b'[]', b'{"a":1,"a":2}', b'{"b":1, "a":2}', b'{"x":NaN}'])
def test_receiver_refuses_noncanonical_or_invalid_json(body):
    receiver = _EvidenceReceiver()
    receiver.accept(_start(body))
    receiver.accept(_part(body))
    with pytest.raises(ContractError, match='Invalid inference evidence response'):
        receiver.accept({'version': 1, 'status': 'evidence_end', 'phase': 'request'})


def test_receiver_rejects_corruption_after_complete_transfer():
    body = b'{"safe":true}'
    receiver = _EvidenceReceiver()
    start = _start(body)
    start['sha256'] = '0' * 64
    receiver.accept(start)
    receiver.accept(_part(body))
    with pytest.raises(ContractError, match='Invalid inference evidence response'):
        receiver.accept({'version': 1, 'status': 'evidence_end', 'phase': 'request'})


def test_receiver_accepts_complete_canonical_transfer():
    body = b'{"safe":true}'
    receiver = _EvidenceReceiver()
    receiver.accept(_start(body))
    receiver.accept(_part(body))
    result = receiver.accept({'version': 1, 'status': 'evidence_end', 'phase': 'request'})
    assert result['content'] == {'safe': True}
    assert result['phase'] == 'request'


def test_evidence_size_guard_rejects_cycles_and_excessive_nesting():
    recursive = []
    recursive.append(recursive)
    assert not _bounded_evidence(recursive, 1024)
    nested = 'value'
    for _ in range(22):
        nested = [nested]
    assert not _bounded_evidence(nested, 1024)
    assert _bounded_evidence({'value': [1, True, None]}, 1024)
