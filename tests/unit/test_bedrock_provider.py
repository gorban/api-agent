import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from api_agent.llm_providers.bedrock import factory as module
from api_agent.providers.contracts import ContractError
from api_agent.providers.costs import validate_quote
from api_agent.providers.llm import AvailableSelection, CompletionRequest, LLMRequest, LLMResult, Message, TextContent, StreamEvent

MODEL = 'zai.glm-4.7-flash'


@pytest.fixture
def controlled(monkeypatch):
    result = LLMResult(Message('assistant', (TextContent('answer'),)))
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),)))
    transport = SimpleNamespace(call=AsyncMock(return_value={'raw': True}), aclose=AsyncMock())
    released = []
    async def stream(*args, **kwargs):
        try:
            yield {'part': 1}
        finally:
            released.append(True)
    transport.stream = stream
    monkeypatch.setattr(module, 'SDKTransport', lambda **kw: transport)
    codec = types.ModuleType('api_agent.llm_providers.bedrock.mantle')
    codec.validate_request = lambda request: None
    codec.encode = lambda request, mode: {'modelId': request.selected_llm}
    codec.decode = lambda response, mode, **kw: result
    class Decoder:
        def __init__(self, mode, **kwargs):
            assert kwargs == {'selected_llm': MODEL}
        def feed(self, event):
            assert event == {'part': 1}
            return (StreamEvent(TextContent('answer')),)
        def finish(self):
            return result
    codec.StreamDecoder = Decoder
    monkeypatch.setitem(sys.modules, codec.__name__, codec)
    return request, result, transport, released


def test_setup_catalog_completion_stream_and_cleanup(controlled, monkeypatch):
    request, result, transport, released = controlled
    session = SimpleNamespace(aclose=AsyncMock())
    create = AsyncMock(return_value=session)
    monkeypatch.setattr(module.auth, 'create_session', create)
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        assert await provider.asetup(interactive=False, user_index=None, credentials=None) is session
        create.assert_awaited_once_with({'BEDROCK_API_MODE': 'completions'}, interactive=False, user_index=None, credentials=None, services={})
        transport.call.return_value = {'data': []}
        assert await provider.adescribe(auth=session) == ()
        transport.call.assert_awaited_with('models', {}, auth=session)
        assert await provider.acomplete(request, auth=session) == result
        transport.call.assert_awaited_with('completions', {'prefix': '/v1', 'body': {'modelId': MODEL}}, auth=session)
        assert [event async for event in provider.astream(request, auth=session)] == [
            StreamEvent(TextContent('answer')), StreamEvent(result, 'completed')]
        assert released == [True]
        assert validate_quote(provider.quote_cost(request, result))['accounting']['billing_mode'] == 'metered'
        await provider.aclose()
        await provider.aclose()
        session.aclose.assert_awaited_once()
        assert provider.sessions == []
        with pytest.raises(ContractError, match='closed'):
            provider.validate_request(request)
    asyncio.run(run())


def test_unknown_model_fails_before_transport(controlled):
    request, _, transport, _ = controlled
    unknown = LLMRequest('unqualified', request.payload)
    async def run():
        with pytest.raises(ContractError, match='Unsupported'):
            await module.factory({'BEDROCK_API_MODE': 'completions'}, {}).acomplete(unknown)
        transport.call.assert_not_awaited()
    asyncio.run(run())


def test_partial_stream_and_decoder_failure_release_transport(controlled, monkeypatch):
    request, _, transport, released = controlled
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        stream = provider.astream(request)
        await anext(stream)
        await stream.aclose()
        assert released == [True]
        codec = sys.modules['api_agent.llm_providers.bedrock.mantle']
        def fail(self, event):
            raise ContractError('Invalid controlled stream')
        monkeypatch.setattr(codec.StreamDecoder, 'feed', fail)
        with pytest.raises(ContractError, match='controlled'):
            await anext(provider.astream(request))
        assert released == [True, True]
    asyncio.run(run())


@pytest.mark.parametrize('close_error', [None, RuntimeError('cleanup pending'), asyncio.CancelledError()])
def test_setup_closing_during_acquisition_disposes_or_retains_late_session(controlled, monkeypatch, close_error):
    async def run():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        session = SimpleNamespace(aclose=AsyncMock(side_effect=close_error))
        async def create(*args, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return session
        monkeypatch.setattr(module.auth, 'create_session', create)
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
        await entered.wait()
        close = asyncio.create_task(provider.aclose())
        await cancelled.wait()
        assert not close.done()
        release.set()
        if close_error is None:
            await close
        else:
            with pytest.raises(ContractError, match='retry disposal'):
                await close
        with pytest.raises(ContractError, match='closed'):
            await setup
        session.aclose.assert_awaited_once()
        assert provider.sessions == ([] if close_error is None else [session])
        session.aclose.side_effect = None
        await provider.aclose()
        assert provider.sessions == []
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['close', 'continue'])
def test_cross_task_stream_boundary(controlled, boundary):
    request, result, _, released = controlled
    async def run():
        stream = module.factory({'BEDROCK_API_MODE': 'completions'}, {}).astream(request)
        assert (await asyncio.create_task(anext(stream))).kind == 'text'
        if boundary == 'close':
            await asyncio.create_task(stream.aclose())
        else:
            assert (await asyncio.create_task(anext(stream))).payload == result
            with pytest.raises(StopAsyncIteration):
                await asyncio.create_task(anext(stream))
        assert released == [True]
    asyncio.run(run())


@pytest.mark.parametrize('source', ['cli', 'environment', 'dotenv', 'clear'])
def test_actual_provider_schema_account_selection(source):
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    selector = str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory'
    # The ordinary selector is a module file location, not a replaced factory.
    override = {'LLM_PROVIDER': selector}
    args = ['--unattended-user-index=0'] if source == 'cli' else ['--unattended-user-index='] if source == 'clear' else []
    environment = {'UNATTENDED_USER_INDEX': '1'} if source in ('environment', 'clear') else {}
    dotenv = {'UNATTENDED_USER_INDEX': '2'} if source == 'dotenv' else {}
    schema = bootstrap_schema(load_shared_schema(), ['--help', *args], environment, dotenv, override, owner_root=root)
    config = resolve_config(schema, argv=args, environ=environment, dotenv=dotenv, overrides=override)
    assert config.values['UNATTENDED_USER_INDEX'] == {'cli': 0, 'environment': 1, 'dotenv': 2, 'clear': None}[source]
    assert config.values['BEDROCK_PROFILE'] == 'default'
    assert config.values['BEDROCK_REGION'] == 'us-east-1'


@pytest.mark.parametrize(('settings', 'profile', 'region'), [
    ({}, 'default', 'us-east-1'),
    ({'BEDROCK_PROFILE': 'chosen', 'BEDROCK_REGION': 'eu-west-1'}, 'chosen', 'eu-west-1'),
    ({'BEDROCK_PROFILE': '', 'BEDROCK_REGION': ''}, None, None),
])
def test_actual_schema_profile_and_region_reach_auth_worker(settings, profile, region):
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    override = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory', **settings}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, override, owner_root=root)
    config = resolve_config(schema, argv=[], environ={}, dotenv={}, overrides=override)

    async def run():
        provider = module.factory(config.values, {})
        session = await provider.asetup(interactive=False, user_index=None, credentials=None)
        assert session.worker_config() == {'mode': 'access_key', 'profile': profile, 'region': region, 'access_key': 'synthetic-key', 'secret_key': 'synthetic-secret', 'session_token': None}
        await provider.aclose()
    asyncio.run(run())


def test_shared_provider_help_identifies_default_and_bedrock_selector():
    from pathlib import Path
    from api_agent.config import load_shared_schema
    field = load_shared_schema().fields['LLM_PROVIDER']
    default_selector = 'src/api_agent/llm_providers/gpt/factory.py:factory'
    path, symbol = field.default.rsplit(':', 1)
    assert Path(path).samefile(Path(__file__).resolve().parents[2] / default_selector.rsplit(':', 1)[0])
    assert symbol == 'factory'
    assert 'default is bundled GPT' in field.help
    assert default_selector in field.help
    assert 'src/api_agent/llm_providers/bedrock/factory.py:factory' in field.help


@pytest.mark.parametrize('stage', ['read', 'result'])
def test_stream_deadline_prevents_subsequent_owned_work(controlled, monkeypatch, stage):
    from contextlib import asynccontextmanager
    request, _, transport, released = controlled
    reads = []
    class Budget:
        @property
        def remaining(self):
            return 0 if stage == 'read' or len(reads) == 2 else 60
    original = module.component_timeout
    @asynccontextmanager
    async def timeout(name, value):
        async with original(name, value):
            yield Budget()
    monkeypatch.setattr(module, 'component_timeout', timeout)
    async def stream(*args, **kwargs):
        reads.append(True)
        yield {'part': 1}
        reads.append(True)
    transport.stream = stream
    async def run():
        with pytest.raises(TimeoutError, match='deadline'):
            _ = [event async for event in module.factory({'BEDROCK_API_MODE': 'completions'}, {}).astream(request)]
        assert len(reads) == (0 if stage == 'read' else 2)
    asyncio.run(run())


@pytest.mark.parametrize('streaming', [False, True])
def test_real_codec_rejects_response_tool_not_in_request(monkeypatch, streaming):
    from api_agent.providers.contracts import ProviderAccessError
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),)))
    response = {'model': MODEL, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'id', 'type': 'function', 'function': {'name': 'unrequested', 'arguments': '{}'}}]},
        'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    transport = SimpleNamespace(call=AsyncMock(return_value=response), aclose=AsyncMock())
    async def stream(*args, **kwargs):
        for delta, finish in [({'role': 'assistant'}, None),
            ({'tool_calls': [{'index': 0, 'id': 'id', 'type': 'function', 'function': {'name': 'unrequested', 'arguments': '{}'}}]}, None),
            ({}, 'tool_calls')]:
            yield {'id': 'r', 'object': 'chat.completion.chunk', 'model': MODEL,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        yield {'id': 'r', 'object': 'chat.completion.chunk', 'model': MODEL, 'choices': [], 'usage': response['usage']}
    transport.stream = stream
    monkeypatch.setattr(module, 'SDKTransport', lambda **kw: transport)
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        with pytest.raises(ProviderAccessError):
            if streaming:
                _ = [event async for event in provider.astream(request)]
            else:
                await provider.acomplete(request)
    asyncio.run(run())


def selected_model_config(provider, *, argv=(), environ=None, dotenv=None, overrides=None):
    """Resolve the real provider declarations through inert discovery."""
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    settings = {'LLM_PROVIDER': str(root / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory', **(overrides or {})}
    environment, file_values = environ or {}, dotenv or {}
    schema = bootstrap_schema(load_shared_schema(), ['--help', *argv], environment, file_values, settings, owner_root=root)
    return schema, resolve_config(schema, argv=argv, environ=environment, dotenv=file_values, overrides=settings)


def test_model_defaults_follow_selected_provider_without_loading_factory(monkeypatch):
    import importlib
    from api_agent.providers.selection import ModelSelectionPolicy
    def reject_import(*args, **kwargs):
        raise AssertionError('Declaration discovery imported a factory')
    monkeypatch.setattr(importlib, 'import_module', reject_import)
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', reject_import)
    patterns = []
    for provider, admitted, denied in [
        ('gpt', 'gpt-6.1-sol', 'zai.glm-5'),
        ('bedrock', 'zai.glm-5', 'gpt-6.1-sol'),
        ('gpt', 'gpt-6.1-sol', 'zai.glm-5'),
    ]:
        schema, config = selected_model_config(provider)
        assert not config.errors
        policy = ModelSelectionPolicy(config.values)
        assert policy.filter((AvailableSelection(admitted), AvailableSelection(denied))) == (AvailableSelection(admitted),)
        assert schema.fields['LLM_ALLOWED_MODELS'].validation == 'regex'
        patterns.append(config.values['LLM_ALLOWED_MODELS'])
    assert patterns[0] == patterns[2] != patterns[1]


@pytest.mark.parametrize('identifier', [
    'openai.gpt-oss-20b', 'openai.gpt-oss-120b-1:0', 'openai.gpt-oss-20b-1:0', 'openai.gpt-6.1-sol',
    'openai.gpt-7.12-luna', 'zai.glm-5', 'zai.glm-4.7', 'zai.glm-4.7-flash',
    'zai.glm-6.12-flash', 'anthropic.claude-haiku-4-5-20251001-v1:0',
    'anthropic.claude-sonnet-4-6', 'anthropic.claude-opus-5.12',
    'anthropic.claude-3-5-sonnet-20241022-v2:0',
    'anthropic.claude-3-haiku-20240307-v1:0', 'anthropic.claude-3-opus-20240229-v1:0',
])
def test_bedrock_default_filters_catalog_with_version_wildcards(identifier):
    from api_agent.providers.selection import ModelSelectionPolicy
    _, config = selected_model_config('bedrock')
    allowed = AvailableSelection(identifier)
    assert ModelSelectionPolicy(config.values).filter((AvailableSelection('other.model'), allowed)) == (allowed,)


@pytest.mark.parametrize('identifier', [
    'amazon.nova-micro-v1:0', 'other.gpt-6-sol', 'gpt-6-sol', 'openai/gpt-6-sol',
    'us.openai.gpt-oss-120b-1:0', 'global.anthropic.claude-sonnet-4-6',
    'eu.zai.glm-5', 'zai.glm-flash', 'zai.glm-5-fable', 'zai.glm-5..1',
    'zai.glm-5-flash-extra', 'anthropic.claude-fable-5', 'anthropic.claude-mythos-5',
    'anthropic.claude-5-mythos', 'anthropic.claude-sonnet', 'anthropic.claude-sonnet-4-extra',
    'anthropic.claude-3-5-sonnet-extra', 'anthropic.claude-3-5-fable-20241022-v1:0',
])
def test_bedrock_default_rejects_other_families_and_profile_ids(identifier):
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.selection import ModelSelectionPolicy
    _, config = selected_model_config('bedrock')
    policy = ModelSelectionPolicy(config.values)
    assert policy.filter((AvailableSelection(identifier),)) == ()
    with pytest.raises(ProviderAccessError, match='not permitted'):
        policy.require_allowed(identifier)


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli', 'runtime_override'])
def test_explicit_model_pattern_replaces_provider_default(provider, source):
    from api_agent.providers.selection import ModelSelectionPolicy
    pattern = r'custom\*|other\.model'
    kwargs = {
        'dotenv': {'dotenv': {'LLM_ALLOWED_MODELS': pattern}},
        'environment': {'dotenv': {'LLM_ALLOWED_MODELS': 'ignored'}, 'environ': {'LLM_ALLOWED_MODELS': pattern}},
        'cli': {'dotenv': {'LLM_ALLOWED_MODELS': 'ignored'}, 'environ': {'LLM_ALLOWED_MODELS': 'ignored'}, 'argv': [f'--llm-allowed-models={pattern}']},
        'runtime_override': {'overrides': {'LLM_ALLOWED_MODELS': pattern}},
    }[source]
    _, config = selected_model_config(provider, **kwargs)
    assert not config.errors
    assert config.values['LLM_ALLOWED_MODELS'] == pattern
    assert ModelSelectionPolicy(config.values).filter((AvailableSelection('other.model'), AvailableSelection('zai.glm-5'))) == (AvailableSelection('other.model'),)


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
@pytest.mark.parametrize('pack_source', ['manifest_default', 'config_declaration'])
def test_selected_provider_default_wins_over_composed_pack_defaults(provider, pack_source):
    """Pack declaration defaults follow the existing provider contribution order."""
    from pathlib import Path
    from api_agent.config import compose_schema, load_shared_schema, resolve_config, schema_from_dict
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    shared = load_shared_schema()
    pack_pattern = 'custom-only'
    if pack_source == 'manifest_default':
        foundation = compose_schema(shared, overrides={'LLM_ALLOWED_MODELS': pack_pattern})
    else:
        declaration = schema_from_dict({'version': 1, 'fields': {'LLM_ALLOWED_MODELS': {
            'type': 'str', 'override': True, 'default': pack_pattern}}}, source='pack_default')
        foundation = compose_schema(shared, declaration)
    assert foundation.fields['LLM_ALLOWED_MODELS'].default == pack_pattern
    selector = str(root / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory'
    schema = bootstrap_schema(foundation, ['--help'], {}, {}, {'LLM_PROVIDER': selector}, owner_root=root)
    config = resolve_config(schema, environ={})
    _, ordinary = selected_model_config(provider)
    assert config.values['LLM_ALLOWED_MODELS'] == ordinary.values['LLM_ALLOWED_MODELS']
    assert config.values['LLM_ALLOWED_MODELS'] != pack_pattern


@pytest.fixture(autouse=True)
def frozen_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    async def acquire(self, request, **kwargs):
        return SessionMaterial('frozen', access_key='synthetic-key', secret_key='synthetic-secret'), {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)


@pytest.mark.parametrize('value', [True, False, 0, -1, float('inf'), float('nan'), '240', None, 3601, 10**1000, [], {}])
def test_inference_timeout_configuration_rejected_before_transport(monkeypatch, value):
    monkeypatch.setattr(module, 'SDKTransport', lambda **kw: pytest.fail('transport constructed'))
    with pytest.raises(ContractError, match='inference timeout'):
        module.factory({'BEDROCK_INFERENCE_TIMEOUT_SECONDS': value}, {})


@pytest.mark.parametrize('value', [None, 120, 900])
def test_inference_timeout_configuration_reaches_transport(monkeypatch, value):
    seen = []
    monkeypatch.setattr(module, 'SDKTransport', lambda **kw: seen.append(kw))
    config = {} if value is None else {'BEDROCK_INFERENCE_TIMEOUT_SECONDS': value}
    provider = module.factory(config, {})
    assert provider.inference_timeout == (900 if value is None else value)
    assert seen == [{'timeout': provider.inference_timeout}]


def test_stream_consumer_pause_uses_original_total_budget(controlled, monkeypatch):
    from api_agent.providers import progress
    request, _, _, released = controlled
    clock = [10.0]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])
    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions',
                                   'BEDROCK_INFERENCE_TIMEOUT_SECONDS': .5}, {})
        stream = provider.astream(request)
        assert (await anext(stream)).kind == 'text'
        clock[0] += 1
        with pytest.raises(TimeoutError, match='deadline'):
            await anext(stream)
        assert released == [True]
    asyncio.run(run())


@pytest.mark.parametrize('configured', [None, 120, 900])
def test_inference_scopes_use_configured_deadline(controlled, configured):
    from api_agent.providers.progress import progress_sink
    request, _, _, _ = controlled
    config = {'BEDROCK_API_MODE': 'completions'}
    if configured is not None:
        config['BEDROCK_INFERENCE_TIMEOUT_SECONDS'] = configured
    reports = []
    async def run():
        provider = module.factory(config, {})
        with progress_sink(reports.append):
            await provider.acomplete(request)
            _ = [event async for event in provider.astream(request)]
    asyncio.run(run())
    expected = 900 if configured is None else configured
    started = {report.name: report.timeout for report in reports if report.active}
    assert started['Bedrock completion'] == expected
    assert started['Bedrock streaming completion'] == expected


@pytest.mark.parametrize('timeout', [None, 120, 900])
def test_resolved_bedrock_deadlines_preserve_explicit_override(timeout):
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    override = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory'}
    if timeout is not None:
        override['BEDROCK_INFERENCE_TIMEOUT_SECONDS'] = str(timeout)
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, override, owner_root=root)
    config = resolve_config(schema, argv=[], environ={}, dotenv={}, overrides=override)
    provider = module.factory(config.values, {})
    assert config.values['BEDROCK_INFERENCE_TIMEOUT_SECONDS'] == (900 if timeout is None else timeout)
    assert provider.inference_timeout == provider.transport.timeout == config.values['BEDROCK_INFERENCE_TIMEOUT_SECONDS']
    assert config.values['LLM_REASONING_TEST_TIMEOUT_SECONDS'] == 960
    assert config.values['LLM_SETUP_REQUEST_TIMEOUT_SECONDS'] == 960


def test_valid_streamed_tool_is_actionable_only_after_transport_eof(monkeypatch):
    from api_agent.providers.llm import Tool, ToolCall
    request = LLMRequest(MODEL, CompletionRequest(
        (Message('user', (TextContent('hello'),)),),
        tools=(Tool('lookup', 'Synthetic lookup', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'),)))
    ended = []

    async def stream(*args, **kwargs):
        for delta, finish in [({'role': 'assistant'}, None),
            ({'tool_calls': [{'index': 0, 'id': 'id', 'type': 'function',
                'function': {'name': 'lookup', 'arguments': '{}'}}]}, None),
            ({}, 'tool_calls')]:
            yield {'id': 'r', 'object': 'chat.completion.chunk', 'model': MODEL,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        yield {'id': 'r', 'object': 'chat.completion.chunk', 'model': MODEL, 'choices': [],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
        ended.append(True)

    transport = SimpleNamespace(stream=stream, aclose=AsyncMock())
    monkeypatch.setattr(module, 'SDKTransport', lambda **kw: transport)

    async def run():
        provider = module.factory({'BEDROCK_API_MODE': 'completions'}, {})
        actionable = []
        async for event in provider.astream(request):
            if event.kind == 'tool_call':
                assert ended == [True]
                actionable.append(event.payload)
        assert actionable == [ToolCall('id', 'lookup', '{}')]
        await provider.aclose()

    asyncio.run(run())
