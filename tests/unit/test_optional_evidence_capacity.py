"""Optional post-redaction capacity is independent of mandatory IPC results."""
import asyncio
import json
import pytest
from api_agent.providers.setup_transport import (MAX_CONTENT_FRAME,MAX_FRAME,
    SetupClient,_content_frame,_encode)
from api_agent.providers.inference import result_to_wire
from api_agent.providers.llm import LLMResult,Message,TextContent,Usage
from test_setup_transport import CAPABILITY


@pytest.mark.parametrize('character',['x','☃','🧪'])
def test_optional_encoded_capacity_includes_envelope_unicode_and_newline(character):
    evidence={'phase':'response','content':{'text':''}}
    base=_content_frame(evidence)
    capacity=MAX_CONTENT_FRAME-len(base)
    width=len(json.dumps(character).encode())-2
    text=character*(capacity//width)+'x'*(capacity%width)
    evidence['content']['text']=text
    frame=_content_frame(evidence)
    assert len(frame) == MAX_CONTENT_FRAME
    assert frame.endswith(b'\n')
    assert json.loads(frame)['content']['text'] == text
    evidence['content']['text']+='x'
    assert len(_encode(dict(version=1,status='evidence',**evidence))) == MAX_CONTENT_FRAME+1
    assert json.loads(_content_frame(evidence)) == dict(version=1, status='evidence', phase='response', content={}, reason='truncated')
    assert MAX_CONTENT_FRAME == 131072 and MAX_FRAME == 4194304


@pytest.mark.parametrize('phase',['request','response'])
def test_received_optional_oversize_is_omitted_without_losing_completed_result(phase):
    expected=LLMResult(Message('assistant',(TextContent('Original answer'),)),Usage(10,4,2,1,3))
    oversized={'version':1,'status':'evidence','phase':phase,'content':{'text':'x'*MAX_CONTENT_FRAME}}
    accepted={'version':1,'status':'evidence','phase':'response' if phase == 'request' else 'request','content':{'text':'Small optional evidence'}}
    class Writer:
        def __init__(self):self.writes=[]
        def write(self,raw):self.writes.append(raw)
        async def drain(self):pass
    async def scenario():
        client=SetupClient({'version':1,'host':'127.0.0.1','port':43123,'capability':CAPABILITY})
        client._writer=Writer()
        client._reader=asyncio.StreamReader(limit=MAX_FRAME)
        client._reader.feed_data(_encode(oversized)+_encode(accepted)+_encode({'version':1,'status':'completed','result':result_to_wire(expected)}))
        client._reader.feed_eof()
        actual=await client._exchange('complete',{})
        assert actual == expected
        assert client.evidence == [dict(phase=phase, content={}, reason='truncated'),
                                   {'phase':accepted['phase'],'content':accepted['content']}]
        assert len(client._writer.writes) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid',['shape','duplicate','reason'])
def test_optional_capacity_does_not_weaken_malformed_protocol_errors(invalid):
    from api_agent.providers.contracts import ContractError
    accepted={'version':1,'status':'evidence','phase':'response','content':{'text':'Small response'}}
    malformed = accepted | {'content': []} if invalid == 'shape' else accepted | {'reason': 'fabricated_complete'} if invalid == 'reason' else accepted
    class Writer:
        def write(self,raw):pass
        async def drain(self):pass
    async def scenario():
        client=SetupClient({'version':1,'host':'127.0.0.1','port':43123,'capability':CAPABILITY})
        client._writer=Writer()
        client._reader=asyncio.StreamReader(limit=MAX_FRAME)
        client._reader.feed_data((_encode(accepted) if invalid == 'duplicate' else b'')+_encode(malformed))
        client._reader.feed_eof()
        with pytest.raises(ContractError,match='Invalid inference evidence response'):
            await client._exchange('complete',{})
    asyncio.run(scenario())
