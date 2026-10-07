import sys
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch
from threading import Event
import pytest
from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceError, TraceRuntime, _sdk_factory


def config(mode='OTEL', content=None, **values):
    fields={} if content is None else {'LOG_CONTENT':ResolvedValue(content,'cli',True)}
    return ConfigResult({'TRACE_MODE':mode, **({} if content is None else {'LOG_CONTENT':content}), **values},fields,(),(),())


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
        with pytest.raises(TraceError,match=r'^Tracing shutdown failed\.$') as caught:
            await runtime.aclose()
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
            with pytest.raises(TraceError, match=r'^Tracing shutdown failed\.$'):
                await runtime.aclose()
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
