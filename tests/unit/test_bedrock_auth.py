import pytest
from api_agent.llm_providers.bedrock.auth import create_session
from api_agent.providers.contracts import ContractError, InteractionRequiredError, SessionClosedError


@pytest.mark.asyncio
async def test_session_is_lazy_opaque_and_disposable():
    session = await create_session({'BEDROCK_PROFILE': 'synthetic'}, interactive=False, user_index=None, credentials=None)
    assert await session() is session
    assert session.worker_config() == {'mode': 'access_key', 'profile': 'synthetic', 'region': None, 'access_key': 'synthetic-key', 'secret_key': 'synthetic-secret', 'session_token': None}
    assert 'synthetic' not in repr(session)
    copy = session.worker_config()
    copy['profile'] = 'changed'
    assert session.worker_config()['profile'] == 'synthetic'
    await session.aclose()
    await session.aclose()
    with pytest.raises(SessionClosedError):
        await session()


@pytest.mark.asyncio
async def test_access_key_uses_only_selected_reader():
    calls = []
    async def reader(index, kind):
        calls.append((index, kind))
        return {'username': 'synthetic-key', 'password': 'synthetic-secret'}[kind]
    session = await create_session({'BEDROCK_AUTH_MODE': 'access_key', 'BEDROCK_REGION': 'region'}, interactive=True, user_index=2, credentials=reader)
    assert calls == [(2, 'username'), (2, 'password')]
    assert session.worker_config()['secret_key'] == 'synthetic-secret'
    assert 'synthetic' not in repr(session)


@pytest.mark.asyncio
@pytest.mark.parametrize('config,index,reader,error', [
    ({}, 0, None, InteractionRequiredError),
    ({'BEDROCK_AUTH_MODE': 'other'}, None, None, ContractError),
    *[({'BEDROCK_AUTH_MODE': 'access_key'}, i, None, InteractionRequiredError) for i in [None, -1, True, 0]],
])
async def test_rejected_source_does_not_fall_back(config, index, reader, error):
    with pytest.raises(error):
        await create_session(config, interactive=False, user_index=index, credentials=reader)


@pytest.mark.asyncio
@pytest.mark.parametrize('key,secret', [(None, 'secret'), ('key', None), ('', 'secret'), ('key', ' '), ('ASIAtemporary', 'secret')])
async def test_incomplete_or_temporary_keys_fail(key, secret):
    async def reader(index, kind):
        return key if kind == 'username' else secret
    with pytest.raises(ContractError):
        await create_session({'BEDROCK_AUTH_MODE': 'access_key'}, interactive=False, user_index=0, credentials=reader)


@pytest.mark.asyncio
async def test_reader_error_is_sanitized():
    async def reader(*args):
        raise ValueError('synthetic-secret')
    with pytest.raises(InteractionRequiredError, match='Unable to read') as error:
        await create_session({'BEDROCK_AUTH_MODE': 'access_key'}, interactive=False, user_index=0, credentials=reader)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.asyncio
async def test_api_key_reads_selected_password_only_and_ignores_profile():
    calls = []
    async def reader(index, kind):
        calls.append((index, kind))
        assert kind == 'password'
        return 'synthetic-bearer'
    session = await create_session({'BEDROCK_AUTH_MODE': 'api_key', 'BEDROCK_PROFILE': 'ignored', 'BEDROCK_REGION': 'us-east-1'}, interactive=False, user_index=3, credentials=reader)
    assert calls == [(3, 'password')]
    assert session.worker_config() == {'mode': 'api_key', 'api_key': 'synthetic-bearer', 'profile': None, 'region': 'us-east-1'}
    assert 'synthetic-bearer' not in repr(session)
    await session.aclose()
    with pytest.raises(SessionClosedError):
        session.worker_config()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['api_key', 'access_key'])
@pytest.mark.parametrize('index', [None, -1, True, False, 0.5, '0'])
async def test_static_auth_requires_selected_account(mode, index):
    calls = []
    async def reader(selected, kind):
        calls.append((selected, kind))
        return {'username': 'synthetic-key', 'password': 'synthetic-secret'}[kind]
    with pytest.raises(InteractionRequiredError):
        await create_session({'BEDROCK_AUTH_MODE': mode}, interactive=False, user_index=index, credentials=reader)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['api_key', 'access_key'])
async def test_static_auth_requires_reader_for_valid_index(mode):
    with pytest.raises(InteractionRequiredError):
        await create_session({'BEDROCK_AUTH_MODE': mode}, interactive=False, user_index=0, credentials=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('secret', [None, 1, '', ' '])
async def test_api_key_rejects_missing_password(secret):
    async def reader(index, kind):
        return secret
    with pytest.raises(ContractError, match='password field'):
        await create_session({'BEDROCK_AUTH_MODE': 'api_key'}, interactive=False, user_index=0, credentials=reader)


@pytest.mark.asyncio
async def test_api_key_reader_error_is_sanitized():
    async def reader(index, kind):
        raise ValueError('synthetic-sensitive-detail')
    with pytest.raises(InteractionRequiredError, match='Unable to read') as error:
        await create_session({'BEDROCK_AUTH_MODE': 'api_key'}, interactive=False, user_index=0, credentials=reader)
    assert 'synthetic-sensitive-detail' not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('BEDROCK_AUTH_MODE', value) for value in [None, [], {}, '', 'invalid']] + [('BEDROCK_API_MODE', value) for value in [None, [], {}, '', 'invalid']])
async def test_invalid_modes_rejected_before_credential_reads(field, value):
    async def reader(*args):
        pytest.fail('invalid configuration must not read credentials')
    config = {'BEDROCK_AUTH_MODE': 'api_key', field: value}
    with pytest.raises(ContractError, match='Unsupported Bedrock'):
        await create_session(config, interactive=False, user_index=0, credentials=reader)


def test_api_modes_validate_and_default():
    from api_agent.llm_providers.bedrock.auth import validate_config
    assert validate_config({}) == 'responses'
    assert validate_config({'BEDROCK_API_MODE': 'completions'}) == 'completions'
    assert validate_config({'BEDROCK_API_MODE': 'responses'}) == 'responses'


def test_provider_declarations_define_mantle_defaults():
    import re
    import tomllib
    from pathlib import Path
    fields = tomllib.loads((Path(__file__).parents[2] / 'src/api_agent/llm_providers/bedrock/cfg/config.toml').read_text())['fields']
    assert fields['BEDROCK_AUTH_MODE']['enum'] == ['session', 'access_key', 'api_key']
    assert fields['BEDROCK_API_MODE']['enum'] == ['completions', 'responses']
    assert fields['BEDROCK_API_MODE']['default'] == 'responses'
    default = fields['LLM_DEFAULT_MODEL']
    assert default['override'] is True and default['override_if_present'] is True
    assert default['default'] == r'openai\.gpt-oss-20b'
    assert 'conditional_defaults' not in default
    assert re.fullmatch(default['default'], 'openai.gpt-oss-20b')
    assert not re.fullmatch(default['default'], 'openai.gpt-oss-120b')
    assert fields['BEDROCK_PROFILE']['default'] == 'default'
    assert fields['BEDROCK_REGION']['default'] == 'us-east-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('index', [-1, True, '0'])
async def test_session_rejects_invalid_selected_index(index):
    with pytest.raises(InteractionRequiredError, match='selected account'):
        await create_session({}, interactive=False, user_index=index, credentials=None)


@pytest.mark.asyncio
async def test_static_session_callable_checks_disposal():
    from api_agent.llm_providers.bedrock.auth import BedrockAuthSession
    session = BedrockAuthSession({'mode': 'api_key', 'api_key': 'synthetic'})
    assert await session() is session
    await session.aclose()
    with pytest.raises(SessionClosedError):
        await session()


@pytest.fixture
def inert_bedrock_schema(monkeypatch):
    import importlib.util
    from pathlib import Path
    from api_agent.config import load_shared_schema
    from api_agent.providers.loading import bootstrap_schema
    def forbidden(*args, **kwargs):
        pytest.fail('schema discovery must not load provider factory')
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', forbidden)
    root = Path(__file__).parents[2]
    selector = str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory'
    return bootstrap_schema(load_shared_schema(), [], {'LLM_PROVIDER': selector}, {}, owner_root=root)


@pytest.mark.parametrize('mode', ['completions', 'responses'])
def test_both_api_modes_default_to_oss20b_inertly(inert_bedrock_schema, mode):
    import re
    from api_agent.config import resolve_config
    resolved = resolve_config(inert_bedrock_schema, environ={'BEDROCK_API_MODE': mode})
    assert not resolved.errors
    pattern = resolved.values['LLM_DEFAULT_MODEL']
    assert pattern == r'openai\.gpt-oss-20b'
    assert re.fullmatch(pattern, 'openai.gpt-oss-20b')
    assert re.fullmatch(resolved.values['LLM_ALLOWED_MODELS'], 'openai.gpt-oss-20b')
    assert not re.fullmatch(pattern, 'openaiXgpt-oss-20b')
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.llm import AvailableSelection
    candidates = tuple(AvailableSelection(identifier) for identifier in
        ('openai.gpt-oss-20b', 'openai.gpt-oss-120b', 'zai.glm-4.7-flash'))
    policy = ModelSelectionPolicy(resolved.values)
    admitted = policy.filter(candidates)
    assert admitted == candidates
    assert policy.select_default(admitted) == 'openai.gpt-oss-20b'


@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('layer', ['dotenv', 'environ', 'argv', 'overrides'])
def test_api_mode_preserves_explicit_default_model(inert_bedrock_schema, mode, layer):
    from api_agent.config import resolve_config
    inputs = {
        'dotenv': {'dotenv': {'LLM_DEFAULT_MODEL': 'explicit-model'}},
        'environ': {'environ': {'LLM_DEFAULT_MODEL': 'explicit-model'}},
        'argv': {'argv': ['--llm-default-model=explicit-model']},
        'overrides': {'overrides': {'LLM_DEFAULT_MODEL': 'explicit-model'}},
    }
    kwargs = inputs[layer]
    environ = {'BEDROCK_API_MODE': mode, **kwargs.get('environ', {})}
    resolved = resolve_config(inert_bedrock_schema, **(kwargs | {'environ': environ}))
    assert not resolved.errors
    assert resolved.values['LLM_DEFAULT_MODEL'] == 'explicit-model'
    assert resolved.values['BEDROCK_API_MODE'] == mode


def test_omitted_api_mode_defaults_to_responses_and_oss20b(inert_bedrock_schema):
    from api_agent.config import resolve_config
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.providers.llm import AvailableSelection
    resolved = resolve_config(inert_bedrock_schema, environ={})
    assert not resolved.errors
    assert resolved.values['BEDROCK_AUTH_MODE'] == 'session'
    assert resolved.values['BEDROCK_API_MODE'] == 'responses'
    candidates = tuple(AvailableSelection(model) for model in
        ('openai.gpt-oss-20b', 'openai.gpt-oss-120b', 'zai.glm-4.7-flash'))
    assert ModelSelectionPolicy(resolved.values).select_default(candidates) == 'openai.gpt-oss-20b'


@pytest.mark.parametrize('saved,mode,model', [
    ('', 'responses', r'openai\.gpt-oss-20b'),
    ('BEDROCK_API_MODE=completions\n', 'completions', r'openai\.gpt-oss-20b'),
    ('BEDROCK_API_MODE=completions\nLLM_DEFAULT_MODEL=chosen-model\n', 'completions', 'chosen-model'),
    ('BEDROCK_API_MODE=responses\nLLM_DEFAULT_MODEL=chosen-model\n', 'responses', 'chosen-model'),
])
def test_config_session_retains_saved_mode_and_model(tmp_path, saved, mode, model):
    from pathlib import Path
    from types import SimpleNamespace
    from api_agent.config import load_shared_schema
    from api_agent.config.session import ConfigSession
    root = Path(__file__).parents[2]
    (tmp_path / '.env').write_text(saved)
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path,
        dotenv_path=tmp_path / '.env', external_pack=False, owner_root=root,
        anchors={'base_root': tmp_path, 'pack_root': tmp_path, 'owner_root': root})
    selector = str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory'
    session = ConfigSession(context, load_shared_schema(), environ={'LLM_PROVIDER': selector})
    resolved = session.result()
    assert not resolved.errors
    assert resolved.values['BEDROCK_API_MODE'] == mode
    assert resolved.values['LLM_DEFAULT_MODEL'] == model
    assert (tmp_path / '.env').read_text() == saved


@pytest.fixture(autouse=True)
def frozen_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    async def acquire(self, request, **kwargs):
        return SessionMaterial('frozen', access_key='synthetic-key', secret_key='synthetic-secret'), {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,failing_kind,expected_kinds', [
    ('api_key', 'password', ['password']),
    ('access_key', 'username', ['username']),
    ('access_key', 'password', ['username', 'password']),
])
async def test_static_auth_preserves_selected_credential_failure(mode, failing_kind, expected_kinds):
    from api_agent.providers.contracts import UnattendedCredentialError
    failure = UnattendedCredentialError('Credential retrieval unavailable.', index_source='dotenv')
    calls = []
    async def reader(index, kind):
        calls.append((index, kind))
        if kind == failing_kind:
            raise failure
        return 'synthetic-key'

    with pytest.raises(UnattendedCredentialError) as caught:
        await create_session({'BEDROCK_AUTH_MODE': mode}, interactive=False,
                             user_index=6, credentials=reader)

    assert caught.value is failure
    assert caught.value.index_source == 'dotenv'
    assert calls == [(6, kind) for kind in expected_kinds]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['api_key', 'access_key'])
async def test_static_auth_redacts_arbitrary_callback_error_without_fallback(mode):
    calls = []
    async def reader(index, kind):
        calls.append((index, kind))
        raise RuntimeError('synthetic-private-callback-value')

    with pytest.raises(InteractionRequiredError) as caught:
        await create_session({'BEDROCK_AUTH_MODE': mode}, interactive=False,
                             user_index=6, credentials=reader)

    assert type(caught.value) is InteractionRequiredError
    assert 'synthetic-private-callback-value' not in str(caught.value)
    assert caught.value.__suppress_context__
    assert calls == [(6, 'password' if mode == 'api_key' else 'username')]
