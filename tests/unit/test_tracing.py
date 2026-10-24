import sys
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch
from threading import Event
import pytest
from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceDeliveryError, TraceError, TraceRuntime, TRACE_DELIVERY_WARNING, _sdk_factory


def config(mode='OTEL', content=None, **values):
    fields={} if content is None else {'TRACELOOP_TRACE_CONTENT':ResolvedValue(content,'cli',True)}
    return ConfigResult({'TRACE_MODE':mode, **({} if content is None else {'TRACELOOP_TRACE_CONTENT':content}), **values},fields,(),(),())


def fake():
    span=Mock()
    provider=Mock(force_flush=Mock(return_value=True))
    tracer=Mock(start_span=Mock(return_value=span))
    return provider,tracer,span,Mock(return_value=(provider,tracer))


@pytest.mark.parametrize('content',[None,False,True])
def test_payloads_cross_boundary_before_recording(content):
    provider,tracer,span,factory=fake()
    runtime=TraceRuntime(config(content=content),factory=factory)
    asyncio.run(runtime.astart())
    runtime.boundary.register_secret('private')
    identifier='00000000-0000-4000-8000-000000000001'
    runtime.boundary.register_id(identifier)
    payload={'prompt':'private prompt','Authorization':'private','request_id':identifier,'input_tokens':3,'duration_ms':4,'status':'ok'}
    with runtime.span(payload) as safe:
        safe.event(payload)
        safe.attributes({'response':'private answer'})
    attrs=tracer.start_span.call_args.kwargs['attributes']
    assert attrs['request_id']==identifier and attrs['input_tokens']==3
    assert 'Authorization' not in attrs
    assert attrs.get('prompt') == ('[REDACTED] prompt' if content else None)
    assert span.add_event.call_args.kwargs['attributes']==attrs
    assert span.set_attributes.call_args.args[0]==({'response':'[REDACTED] answer'} if content else {})
    assert span.end.call_count==1
    assert runtime.flush() and runtime.shutdown() and runtime.shutdown() and runtime.flush()
    assert provider.shutdown.call_count==1


def test_disabled_no_factory_or_sdk_imports():
    factory=Mock()
    with patch.dict(sys.modules,{'opentelemetry.sdk.trace':None}):
        runtime=TraceRuntime(config(''),factory=factory)
        with runtime.span({'prompt':'raw'}) as span:
            span.event({'prompt':'raw'})
            span.attributes({'prompt':'raw'})
        runtime.event({'prompt':'raw'})
        assert runtime.flush() and runtime.shutdown()
    factory.assert_not_called()


def test_langfuse_auth_and_credential_redaction():
    _,tracer,span,factory=fake()
    runtime=TraceRuntime(config('LangFuse',True,TRACE_TARGET='https://example.invalid/',LANGFUSE_PUBLIC_KEY='pk-lf-test',LANGFUSE_SECRET_KEY='sk-lf-test'),factory=factory)
    asyncio.run(runtime.astart())
    endpoint,headers,resource=factory.call_args.args
    assert endpoint=='https://example.invalid/api/public/otel/v1/traces'
    assert headers=={'Authorization':'Basic cGstbGYtdGVzdDpzay1sZi10ZXN0'} and resource=={}
    runtime.event({'payload':headers['Authorization']+' pk-lf-test sk-lf-test'})
    assert 'sk-lf-test' not in str(tracer.mock_calls)
    assert span.add_event.call_args.kwargs['attributes']['payload']=='[REDACTED] [REDACTED] [REDACTED]'


@pytest.mark.parametrize('values,message',[
    ({'TRACE_MODE':'wrong'},'Unsupported'),
    ({'TRACE_TARGET':'file:///tmp/foo'},'Invalid'),
    ({'TRACE_TARGET':'https://user:secret@example.invalid'},'Invalid'),
    ({'TRACE_TARGET':'http://localhost:wrong'},'Invalid'),
    ({'TRACE_TARGET':123},'Invalid'),
    ({'TRACE_MODE':'LangFuse'},'API keys'),
])
def test_invalid_configuration_never_allocates_exporter(values,message):
    factory=Mock()
    mode=values.pop('TRACE_MODE','OTEL')
    with pytest.raises(TraceError,match=message):
        TraceRuntime(config(mode,**values),factory=factory)
    factory.assert_not_called()


def test_factory_failure_sanitized():
    with pytest.raises(TraceError,match='^Tracing initialization failed.$'):
        asyncio.run(TraceRuntime(config(),factory=Mock(side_effect=RuntimeError('secret'))).astart())


def test_exception_is_classification_only_and_original_propagates():
    _,_,span,factory=fake()
    runtime=TraceRuntime(config(content=True),factory=factory)
    asyncio.run(runtime.astart())
    error=ValueError('secret payload')
    with pytest.raises(ValueError) as caught:
        with runtime.span():
            raise error
    assert caught.value is error
    span.set_attributes.assert_called_once_with({'status':'error','error_kind':'invalid_value'})
    span.end.assert_called_once()
    span.record_exception.assert_not_called()


def test_flush_failures_and_bounded_shutdown():
    provider,_,_,factory=fake()
    runtime=TraceRuntime(config(),factory=factory)
    asyncio.run(runtime.astart())
    provider.force_flush.side_effect=RuntimeError('private')
    assert runtime.flush() is False and runtime.shutdown() is False
    assert runtime.shutdown() is False
    provider.shutdown.assert_called_once()
    runtime.event({'status':'ok'})
    with pytest.raises(TraceError,match='timeout'):
        runtime.shutdown(timeout=0)
    blocker=Event()
    runtime=TraceRuntime(config(),factory=fake()[3])
    asyncio.run(runtime.astart())
    runtime._provider.force_flush.side_effect=lambda **kwargs:blocker.wait()
    try:
        assert runtime.shutdown(timeout=.01) is False
        worker=runtime._worker
        assert runtime.shutdown(timeout=.01) is False
        assert runtime._worker is worker
    finally:
        blocker.set()


def test_complex_attributes_are_json_after_sanitizing():
    _,tracer,_,factory=fake()
    runtime=TraceRuntime(config(content=True),factory=factory)
    asyncio.run(runtime.astart())
    runtime.event({'payload':{'authorization':'secret','items':[1,2]},'none':None})
    # Event uses the same boundary and never forwards arbitrary SDK values.
    span=tracer.start_span.return_value
    assert span.add_event.call_args.kwargs['attributes']=={'payload':'{"items": [1, 2]}','none':'null'}


def test_real_owned_sdk_factory_uses_no_global_provider_or_environment_resource():
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.sdk.resources import Resource
    with patch.object(trace,'set_tracer_provider') as global_hook,patch.object(Resource,'create') as detectors,patch('requests.Session') as session:
        provider,tracer=_sdk_factory('https://example.invalid/v1/traces',{}, {})
        assert provider.resource.attributes == {'service.name':'api-agent'}
        exporter=provider._active_span_processor._span_processors[0].span_exporter
        session.return_value.post.return_value.status_code=200
        span=tracer.start_span('api_agent.span',attributes={'status':'ok'})
        span.end()
        assert provider.force_flush(timeout_millis=1000)
        assert session.return_value.trust_env is False
        assert session.return_value.post.call_args.kwargs['timeout']==5
        assert session.return_value.post.call_args.kwargs['allow_redirects'] is False
        assert session.return_value.post.call_args.kwargs['stream'] is True
        session.return_value.post.return_value.close.assert_called_once()
        session.return_value.post.return_value.status_code=403
        assert exporter.export([])==SpanExportResult.FAILURE
        session.return_value.post.side_effect=RuntimeError('private backend response')
        assert exporter.export([])==SpanExportResult.FAILURE
        assert exporter.force_flush()
        provider.shutdown()
        assert exporter.export([])==SpanExportResult.FAILURE
        global_hook.assert_not_called()
        detectors.assert_not_called()


def test_lazy_start_and_owned_async_close():
    provider,_,_,factory=fake()
    runtime=TraceRuntime(config(),factory=factory)
    factory.assert_not_called()
    runtime.event({'status':'ok'})
    async def scenario():
        await runtime.astart()
        await runtime.astart()
        assert await runtime.aclose()
        await runtime.astart()
    asyncio.run(scenario())
    factory.assert_called_once()
    provider.shutdown.assert_called_once()
    disabled=TraceRuntime(config(''),factory=factory)
    asyncio.run(disabled.astart())
    assert asyncio.run(disabled.aclose())


@pytest.mark.parametrize('issue',['missing','errors'])
def test_invalid_config_blocks_lazy_sdk_creation(issue):
    from dataclasses import replace
    factory=Mock()
    with pytest.raises(TraceError,match='valid complete'):
        TraceRuntime(replace(config(),**{issue:('invalid',)}),factory=factory)
    factory.assert_not_called()


def test_explicit_blank_target_rejected_and_shutdown_failure_reported():
    with pytest.raises(TraceError,match='Invalid'):
        TraceRuntime(config(TRACE_TARGET=''))
    provider,_,_,factory=fake()
    runtime=TraceRuntime(config(),factory=factory)
    asyncio.run(runtime.astart())
    provider.shutdown.side_effect=RuntimeError('secret')
    assert not runtime.shutdown()
    assert not runtime.shutdown()


def test_cancelled_aclose_keeps_single_owned_cleanup_worker():
    provider,_,_,factory=fake()
    entered,release=Event(),Event()
    def flushing(**kwargs):
        entered.set()
        release.wait(1)
        return True
    provider.force_flush.side_effect=flushing
    runtime=TraceRuntime(config(),factory=factory)
    async def scenario():
        await runtime.astart()
        task=asyncio.create_task(runtime.aclose(timeout=1))
        while not entered.is_set():
            await asyncio.sleep(.001)
        worker=runtime._worker
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await runtime.aclose(timeout=1)
        assert runtime._worker is worker
    asyncio.run(scenario())
    provider.shutdown.assert_called_once()


def test_async_close_reports_failed_shutdown_with_fixed_diagnostic():
    provider,_,_,factory=fake()
    runtime=TraceRuntime(config(),factory=factory)
    provider.force_flush.return_value=False
    async def scenario():
        await runtime.astart()
        with pytest.raises(TraceDeliveryError) as caught:
            await runtime.aclose()
        assert str(caught.value) == TRACE_DELIVERY_WARNING
        assert caught.value.__cause__ is None
    asyncio.run(scenario())
    provider.shutdown.assert_called_once()


@pytest.mark.parametrize('failure', ['http403', 'network'])
def test_real_sdk_export_failure_propagates_to_flush_and_async_close(failure):
    with patch.dict('os.environ', {'OTEL_TRACES_SAMPLER':'always_off'}), patch('requests.Session') as session:
        if failure == 'http403':
            session.return_value.post.return_value.status_code = 403
        else:
            session.return_value.post.side_effect = OSError('secret network diagnostic')
        runtime = TraceRuntime(config())
        async def scenario():
            await runtime.astart()
            runtime.event({'status': 'ok'})
            assert runtime.flush() is False
            # Failure is sticky even after the queue is fully drained.
            assert runtime.flush() is False
            with pytest.raises(TraceDeliveryError) as caught:
                await runtime.aclose()
            assert str(caught.value) == TRACE_DELIVERY_WARNING
        asyncio.run(scenario())
        session.return_value.close.assert_called_once()
        if failure == 'http403':
            session.return_value.post.return_value.close.assert_called_once()


def test_ambient_sdk_disabled_conflict_allocates_no_owned_resources():
    with patch.dict('os.environ', {'OTEL_SDK_DISABLED':' True '}), \
         patch('requests.Session') as session, \
         patch('opentelemetry.sdk.trace.TracerProvider') as provider:
        runtime=TraceRuntime(config())
        with pytest.raises(TraceError,match=r'^Tracing initialization failed\.$'):
            asyncio.run(runtime.astart())
        session.assert_not_called()
        provider.assert_not_called()
        assert runtime._provider is None
        assert runtime._worker is None
        assert runtime.shutdown()
        disabled=TraceRuntime(config(''))
        asyncio.run(disabled.astart())
        assert asyncio.run(disabled.aclose())


def test_manual_signal_names_and_structural_payloads_are_bounded_and_scrubbed():
    provider,tracer,_,factory = fake()
    runtime = TraceRuntime(config(),factory=factory, model_identities=('synthetic-name-secret',))
    runtime.boundary.register_secret('synthetic-name-secret')
    asyncio.run(runtime.astart())
    span = runtime.begin('chat synthetic-name-secret',{
        'prompt':'withheld-payload', 'gen_ai.request.model':'synthetic-name-secret',
        'gen_ai.usage.input_tokens':True,'api_agent.attempt':-1,
        'api_agent.scope_id':'invalid whitespace', 'gen_ai.operation.name':'unknown'})
    span.end()
    assert 'synthetic-name-secret' not in repr(tracer.start_span.call_args)
    assert 'withheld-payload' not in repr(tracer.start_span.call_args)
    assert tracer.start_span.call_args.kwargs['attributes'] == {'gen_ai.request.model':'[REDACTED]'}
    runtime.begin(object(),{}).end()
    assert tracer.start_span.call_args.args[0] == 'api_agent.span'
    runtime.begin('chat model',None).end()
    runtime.shutdown()


def test_owned_root_context_excludes_ambient_span_and_cancelled_leaf_remains_cancelled():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from api_agent.workflows.observations import WorkflowObservationFactory
    from uuid import uuid4
    from test_workflow_observations import definition
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = TraceRuntime(config(),factory=lambda *args:(provider,provider.get_tracer('synthetic')))
    asyncio.run(runtime.astart())
    with provider.get_tracer('ambient').start_as_current_span('unrelated'):
        adapter = WorkflowObservationFactory(config(),boundary=EmissionBoundary(),trace=runtime).create(definition())
        adapter.observe(dict(event='scope_start',scope_id='node',owner='Make',kind='node'))
        adapter.record_call(visit_id='node',call_id=str(uuid4()),outcome='cancelled')
        adapter.close()
    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name=='node Make')
    leaf = next(span for span in spans if span.kind.name=='CLIENT')
    assert root.parent is None
    assert leaf.parent.span_id == root.context.span_id
    assert leaf.attributes['status'] == 'cancelled'
    runtime.shutdown()


@pytest.mark.parametrize('model',[123,'x'*201,'invalid\nmetadata'])
def test_manual_model_metadata_omission_does_not_restrict_provider_operations(model):
    _,tracer,_,factory = fake()
    runtime = TraceRuntime(config(),factory=factory)
    asyncio.run(runtime.astart())
    runtime.begin('chat model',{'gen_ai.request.model':model}).end()
    assert tracer.start_span.call_args.kwargs['attributes'] == {}
    runtime.shutdown()


def test_manual_span_events_and_attributes_exclude_hidden_serialized_carriers_before_sdk():
    import json
    _,tracer,span,factory = fake()
    runtime = TraceRuntime(config(content=True),factory=factory)
    asyncio.run(runtime.astart())
    payload = {'hidden_reasoning':'excluded-hidden-sentinel','continuation_state':'excluded-hidden-sentinel',
        'serialized':json.dumps({'nested':{'hiddenReasoning':'excluded-hidden-sentinel','text':'visible answer'}}),
        'prompt':'visible question','reasoning_tokens':7,'reasoning_effort':'high'}
    with runtime.span(payload) as capture:
        capture.event(payload)
        capture.attributes(payload)
    assert 'excluded-hidden-sentinel' not in repr(tracer.mock_calls)
    assert 'excluded-hidden-sentinel' not in repr(span.mock_calls)
    attrs = tracer.start_span.call_args.kwargs['attributes']
    assert attrs['prompt']=='visible question' and attrs['reasoning_tokens']==7
    assert attrs['reasoning_effort']=='high'
    assert json.loads(attrs['serialized'])=={'nested':{'text':'visible answer'}}
    runtime.shutdown()


@pytest.mark.parametrize('content', [False, True])
def test_unclassified_model_prose_is_omitted_before_sdk_creation(content):
    _, tracer, _, factory = fake()
    runtime = TraceRuntime(config(content=content), factory=factory)
    asyncio.run(runtime.astart())
    model = 'customer_private_question'

    runtime.begin('chat ' + model, {'gen_ai.request.model': model}).end()

    assert tracer.start_span.call_args.args[0] == 'chat'
    assert tracer.start_span.call_args.kwargs['attributes'] == {}
    assert model not in repr(tracer.mock_calls)
    runtime.shutdown()


@pytest.mark.parametrize('content', [False, True])
@pytest.mark.parametrize('model', ['public/model-v1@latest', 'model with spaces', '模型@v1'])
def test_established_model_identities_remain_structural_without_content_capture(content, model):
    _, tracer, _, factory = fake()
    runtime = TraceRuntime(config(content=content), factory=factory,
                           model_identities=(model,))
    asyncio.run(runtime.astart())

    runtime.begin('chat ' + model, {'gen_ai.request.model': model}).end()

    assert tracer.start_span.call_args.args[0] == 'chat ' + model
    assert tracer.start_span.call_args.kwargs['attributes'] == {'gen_ai.request.model': model}
    runtime.shutdown()


def test_protocol_model_identifier_remains_structural_and_credentials_are_scrubbed():
    _, tracer, _, factory = fake()
    runtime = TraceRuntime(config(content=False), factory=factory,
                           model_identities=('provider/model@credential',))
    runtime.boundary.register_secret('credential')
    asyncio.run(runtime.astart())

    runtime.begin('chat provider/model@credential',
                  {'gen_ai.request.model': 'provider/model@credential'}).end()

    assert tracer.start_span.call_args.args[0] == 'chat provider/model@[REDACTED]'
    assert tracer.start_span.call_args.kwargs['attributes'] == {
        'gen_ai.request.model': 'provider/model@[REDACTED]'}
    runtime.shutdown()


@pytest.mark.parametrize('identities', [None, 'model', {}, ['x'] * 257,
    [None], [''], ['x' * 201], ['invalid\nidentity']])
def test_invalid_explicit_identity_collection_fails_before_sdk_allocation(identities):
    factory = Mock()
    with pytest.raises(TraceError, match='^Trace model identities must be a bounded collection of identifiers\\.$'):
        TraceRuntime(config(), factory=factory, model_identities=identities)
    factory.assert_not_called()


@pytest.mark.parametrize('content', [False, True])
def test_explicit_model_identity_credentials_are_scrubbed_before_sdk(content):
    _, tracer, _, factory = fake()
    runtime = TraceRuntime(config(content=content), factory=factory,
                           model_identities=('model identity credential',))
    runtime.boundary.register_secret('credential')
    asyncio.run(runtime.astart())

    runtime.begin('chat model identity credential',
                  {'gen_ai.request.model': 'model identity credential'}).end()

    assert tracer.start_span.call_args.args[0] == 'chat model identity [REDACTED]'
    assert tracer.start_span.call_args.kwargs['attributes'] == {
        'gen_ai.request.model': 'model identity [REDACTED]'}
    runtime.shutdown()


@pytest.mark.parametrize('content', [False, True])
@pytest.mark.parametrize('admitted', [False, True])
@pytest.mark.parametrize('entry', ['begin', 'span', 'attributes', 'event', 'runtime_event'])
def test_reserved_model_attribute_requires_catalog_admission_in_every_entry(content, admitted, entry):
    _, tracer, sdk_span, factory = fake()
    model = 'customer_private_question'
    runtime = TraceRuntime(config(content=content), factory=factory,
        model_identities=(model,) if admitted else ())
    asyncio.run(runtime.astart())
    payload = {'gen_ai.request.model': model}
    if entry == 'begin':
        runtime.begin('chat ' + model, {}, payload=payload).end()
        actual = tracer.start_span.call_args.kwargs['attributes']
    elif entry == 'runtime_event':
        runtime.event(payload)
        actual = sdk_span.add_event.call_args.kwargs['attributes']
    else:
        with runtime.span(payload if entry == 'span' else {}) as span:
            if entry == 'attributes':
                span.attributes(payload)
                actual = sdk_span.set_attributes.call_args.args[0]
            elif entry == 'event':
                span.event(payload)
                actual = sdk_span.add_event.call_args.kwargs['attributes']
            else:
                actual = tracer.start_span.call_args.kwargs['attributes']
    assert actual == ({'gen_ai.request.model': model} if admitted else {})
    assert runtime.shutdown()
