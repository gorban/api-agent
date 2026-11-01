"""Application adapters preserve complete invocation capture ownership."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.providers.control import OperationContext
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import authority_scope
from api_agent.testing.workflow_capture import capture_sources, capture_config, worker_capture, provider_capture
from api_agent.workflows.capture_authority import bind_capture_services, resolve_capture_services
from api_agent.workflows.observations import WorkflowObservationFactory


def selected_application(kind, text):
    if kind == 'chat':
        from test_local_application import ROOT, load_definition, LocalChatApplication
        definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
        app = LocalChatApplication(definition)
        result = LLMResult(Message('assistant', (TextContent(text),)))
        llm = SimpleNamespace(complete=AsyncMock(return_value=result))
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent(text),)),)))
    else:
        from test_demo_application import application, request as demo_request, Owner, LLM, verdict
        app = application(Owner())
        llm = LLM([json.dumps({'use_tool':False, 'arguments':{}, 'answer':text}), verdict(), text])
        request = demo_request()
    return app, llm, request


@pytest.mark.parametrize('kind', ['chat', 'demo'])
@pytest.mark.parametrize('log,trace', [(False,False),(False,True),(True,False),(True,True)])
def test_application_preserves_full_worker_capture_union(monkeypatch, kind, log, trace):
    from api_agent.workflows import evidence
    journals, rows, traces, acquisitions = [], [], [], []
    text = 'visible ambermarble cobaltriver silvermeadow violetstone'
    app, llm, request = selected_application(kind, text)
    values = {'LOG_CONTENT':log, 'TRACELOOP_TRACE_CONTENT':trace, 'API_KEY':'violetstone'}
    fields = {name:ResolvedValue(values[name], 'cli', True) for name in ('LOG_CONTENT','TRACELOOP_TRACE_CONTENT')}
    snapshot = {'config':values, 'capture_sources':capture_sources(values, fields)}
    remote = EmissionBoundary(ContentPolicy(False), secrets=('ambermarble',))
    class Client:
        async def prepare_graph_capture(self): pass
        @asynccontextmanager
        async def graph_capture_authority(self):
            acquisitions.append(True)
            lease = remote.begin_evidence_lease()
            try: yield remote
            finally: lease.release()
    llm.client = Client()
    class Span:
        def event(self, row): traces.append(row)
        def attributes(self, row): pass
        def end(self, **kwargs): pass
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    monkeypatch.setattr(evidence, 'publish', journals.append)
    async def run():
        with authority_scope(('ui','tools','workflow')) as scope:
            scope.join('ui', EmissionBoundary(ContentPolicy(False), secrets=('cobaltriver',)))
            scope.join('tools', EmissionBoundary(ContentPolicy(False), secrets=('silvermeadow',)))
            with worker_capture(snapshot, scope, ('ui','tools','workflow')):
                with provider_capture(llm):
                    capture = dict(resolve_capture_services({}))
                    trace_owner = SimpleNamespace(boundary=capture['emissions'].for_policy(ContentPolicy(trace)),
                        is_model_identity=lambda _:False, begin=lambda *args,**kwargs:Span())
                    capture['observation_factory'] = WorkflowObservationFactory(
                        capture_config(values, snapshot['capture_sources']), boundary=capture['emissions'],
                        sink=rows.append, trace=trace_owner)
                    with bind_capture_services(capture):
                        result = await app.complete(request, llm=llm, context=OperationContext('request'))
                        assert text in result.payload.content[0].text
    asyncio.run(run())
    assert len(journals) == 1
    captures = [row for row in journals[0]['events'] if row['event']=='capture']
    assert captures and any('values' in row for row in captures) is log
    assert any(row.get('values') for row in traces) is trace
    exported = json.dumps((journals, rows, traces), default=str)
    for secret in ('ambermarble','cobaltriver','silvermeadow','violetstone'):
        assert secret not in exported
    if log or trace:
        assert 'visible' in exported and '[REDACTED]' in exported
    assert acquisitions


@pytest.mark.parametrize('kind', ['chat', 'demo'])
def test_explicit_application_factory_does_not_mix_inherited_authority(monkeypatch, kind):
    from api_agent.workflows import evidence
    journals, rows = [], []
    app, llm, request = selected_application(kind, 'visible owned-secret')
    config = ConfigResult({'LOG_CONTENT':True}, {'LOG_CONTENT':ResolvedValue(True,'cli',True)}, (), (), ())
    app.config['LOG_CONTENT'] = True
    app.observations = WorkflowObservationFactory(config,
        boundary=EmissionBoundary(ContentPolicy(True), secrets=('owned-secret',)), sink=rows.append)
    @asynccontextmanager
    async def forbidden():
        pytest.fail('Explicit application owner borrowed inherited capture authority')
        yield
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    monkeypatch.setattr(evidence, 'publish', journals.append)
    async def run():
        with bind_capture_services({'capture_authority':forbidden, 'content_policy':ContentPolicy(False)}):
            await app.complete(request, llm=llm, context=OperationContext('request'))
    asyncio.run(run())
    assert journals and rows
    assert 'owned-secret' not in json.dumps((journals,rows), default=str)
    assert '[REDACTED]' in json.dumps((journals,rows), default=str)
