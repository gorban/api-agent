"""Credential normalization and pre-decoding allocation guards."""
import asyncio
import gzip
from unittest.mock import Mock

import httpx
import pytest

from api_agent.llm_providers.gpt.discovery import ModelDiscoveryError, _failure, discover_models
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.setup_transport import _error, _raise_error

KEY = 'synthetic-selected-api-key-never-exported'
BASE = 'https://compatible.example/v1'


@pytest.mark.parametrize('control', ['\n', '\0', '\x1b', '\r\n', '\t', '\u200b'])
def test_normalized_diagnostics_never_reassemble_credentials(control):
    reflected = KEY[:10] + control + KEY[10:]
    error = _failure(401, {'error': {'message': 'Rejected: ' + reflected,
        'code': reflected, 'type': reflected}}, reflected, KEY)
    frame = _error(error)
    assert KEY not in repr(vars(error)) + repr(frame)
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(frame)
    assert 'Rejected: [redacted]' in str(caught.value)
    assert caught.value.code == caught.value.type == caught.value.request_id == '[redacted]'
    assert caught.value.status == 401


def test_normalization_precedes_bearer_and_url_redaction():
    error = _failure(403, {'error': {'message': 'Bearer\0 OTHER-SECRET and ht\ntps://private.example/account'}}, None, KEY)
    assert str(error).splitlines()[0] == 'Bearer [redacted] and [endpoint]'
    assert 'OTHER-SECRET' not in str(error) and 'private.example' not in str(error)


def test_final_redaction_catches_credentials_created_by_other_replacements():
    # A Bearer replacement can form a credential at the redaction-marker boundary.
    key = '[redacted],suffix'
    error = _failure(403, {'error': {'message': 'Denied: Bearer other-value,suffix'}}, None, key)
    assert key not in str(error)


def test_marker_collision_omits_detail_instead_of_returning_the_secret():
    key = '[redacted]'
    error = _failure(401, {'error': {'message': '[reda\0cted]', 'code': key, 'type': key}}, key, key)
    assert key not in str(error) + repr(vars(error))
    assert error.message == 'GPT model discovery failed.'
    assert error.code is error.type is error.request_id is None


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def invoke(response):
    requests = []
    def handler(request):
        requests.append(request)
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await discover_models(client=client, base_url=BASE,
                access_token=KEY, subscription=False)
    return asyncio.run(run()), requests


def test_oversized_gzip_is_rejected_before_reading_or_decoding(monkeypatch):
    from httpx._decoders import GZipDecoder
    compressed = gzip.compress(b' ' * (8 * 1024 * 1024))
    assert len(compressed) < 9000
    stream = ObservedStream([compressed])
    response = httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=stream)
    original_decode = GZipDecoder.decode
    decoder = Mock()
    def tracked_decode(self, data):
        decoded = original_decode(self, data)
        decoder(len(decoded))
        return decoded
    monkeypatch.setattr(GZipDecoder, 'decode', tracked_decode)
    with pytest.raises(ModelDiscoveryError, match='unsupported content encoding'):
        invoke(response)
    decoder.assert_not_called()
    assert stream.reads == 0 and stream.closed and response.is_closed


@pytest.mark.parametrize('encoding', ['deflate', 'br', 'zstd', 'unknown', 'gzip, identity'])
def test_nonidentity_encoding_is_rejected_without_reading(encoding):
    stream = ObservedStream([b'not JSON'])
    response = httpx.Response(401, headers={'content-encoding': encoding}, stream=stream)
    with pytest.raises(ModelDiscoveryError, match='unsupported content encoding') as caught:
        invoke(response)
    assert caught.value.kind == 'unknown'
    assert stream.reads == 0 and stream.closed and response.is_closed


@pytest.mark.parametrize('encoding', [None, '', 'identity', ' Identity '])
def test_identity_streams_keep_catalog_and_explicit_encoding_request(encoding):
    stream = ObservedStream([b'{"data":', b'[{"id":"gpt-example"}]}'])
    response = httpx.Response(200, headers={} if encoding is None else {'content-encoding': encoding}, stream=stream)
    rows, requests = invoke(response)
    assert rows[0].selected_llm == 'gpt-example'
    assert requests[0].headers['accept-encoding'] == 'identity'
    assert requests[0].headers['authorization'] == 'Bearer ' + KEY
    assert stream.reads == 2 and stream.closed and response.is_closed


def test_identity_stream_rejects_incremental_oversize_and_closes():
    stream = ObservedStream([b' ' * 1048576, b'x', b'not-read'])
    response = httpx.Response(200, stream=stream)
    with pytest.raises(ModelDiscoveryError, match='exceeded its limit'):
        invoke(response)
    assert stream.reads == 2 and stream.closed and response.is_closed
