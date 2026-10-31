"""Native acquisition selection joins real configuration, flow and LocalLLM offline."""
import asyncio
from pathlib import Path
import time

import pytest

pytestmark = pytest.mark.usefixtures('isolated_native_login_gate')

from api_agent.auth.interaction_transport import OwnedInteraction
from api_agent.config import load_shared_schema, resolve_config
from api_agent.providers.control import OperationContext
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
from api_agent.providers.loading import bootstrap_schema
from api_agent.runtime.local_llm import LocalLLM
from api_agent.workflows.definition import load_definition
from api_agent.workflows.local_application import LocalChatApplication

ROOT = Path(__file__).resolve().parents[2]
MODEL = 'openai.gpt-oss-20b'


class PrivateChannel:
    def __init__(self):
        self.output = []
        self.closed = False

    def write(self, value):
        self.output.append(value)

    def read(self, timeout):
        return None

    def close(self):
        self.output.clear()
        self.closed = True


class PrivateInteraction:
    def __init__(self):
        self.channels = []

    def open(self, label):
        assert label == 'AWS sign-in'
        channel = PrivateChannel()
        self.channels.append(channel)
        return channel


def native_config(method):
    overrides = {
        'LLM_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
        'BEDROCK_SESSION_PROVIDER': str(ROOT / f'src/api_agent/llm_providers/bedrock/{"aws_sso" if method == "sso" else "aws_login"}.py') + ':factory',
        'BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID': '123456789012',
        'BEDROCK_LOGIN_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/example',
        'BEDROCK_LOGIN_SSO_START_URL': 'https://example.com/start',
        'BEDROCK_LOGIN_SSO_REGION': 'us-east-1',
        'BEDROCK_LOGIN_SSO_ROLE_NAME': 'Example',
        'BEDROCK_LOGIN_SSO_ACCOUNT_ID': '123456789012',
        'NO_STD_IN': 'False',
    }
    schema = bootstrap_schema(load_shared_schema(), [], {}, {}, overrides, owner_root=ROOT)
    result = resolve_config(schema, environ={}, overrides=overrides)
    assert not result.errors
    assert result.values['BEDROCK_API_MODE'] == 'responses'
    assert result.values['BEDROCK_PROFILE'] == 'default'
    assert result.values['BEDROCK_REGION'] == 'us-east-1'
    return dict(result.values)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['direct', 'sso'])
@pytest.mark.parametrize('no_stdin', [False, True])
async def test_bundled_native_acquirer_reaches_real_parsed_flow(monkeypatch, method, no_stdin):
    from api_agent.llm_providers.bedrock.login_transport import LoginTransport

    async def forbidden(*args, **kwargs):
        pytest.fail('Offline join cannot launch any AWS/SDK helper')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    private = PrivateInteraction()
    borrowed = OwnedInteraction(private)
    temporary_roots = []
    acquisitions = []

    async def acquire(transport, settings, environment, interaction):
        acquisitions.append(settings)
        root = Path(environment['HOME'])
        temporary_roots.append(root)
        assert root.exists() and settings['method'] == method
        assert settings['profile'] == 'default' and settings['region'] == 'us-east-1'
        assert settings['account'] == '123456789012'
        assert 'AWS_ACCESS_KEY_ID' not in environment
        assert (root / '.aws/credentials').read_text() == ''
        assert interaction is not None
        await interaction.write('Complete the synthetic browser handoff.')
        return dict(AccessKeyId='synthetic-key', SecretAccessKey='synthetic-secret',
                    SessionToken='synthetic-token', expires_at=time.time() + 180, identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))

    async def inference(operation, payload, *, auth, join=None, on_dispatch=None):
        # Mirror the real SDK transport's per-call auth admission.
        values = (await auth()).worker_config()
        assert values['mode'] == 'temporary' and values['session_token'] == 'synthetic-token'
        if join is not None:
            _, lease = await join([values['access_key'], values['secret_key'], values['session_token']])
            try:
                if on_dispatch is not None: on_dispatch()
            finally:
                lease.release()
        if operation == 'models':
            return {'data': [{'id': MODEL}]}
        assert operation == 'responses' and payload['prefix'] == '/v1'
        assert payload['body']['model'] == MODEL and payload['body']['store'] is False
        return dict(id='offline', model=MODEL, status='completed',
                    output=[dict(id='m', type='message', role='assistant', status='completed',
                                 content=[dict(type='output_text', text='Ok', annotations=[])])],
                    usage=dict(input_tokens=3, output_tokens=1, total_tokens=4))

    monkeypatch.setattr(LoginTransport, 'acquire', acquire)
    config = dict(native_config(method), NO_STD_IN=no_stdin)
    owner = LocalLLM(config, ROOT, interaction_owner=borrowed)
    context = OperationContext('offline-native-login', time.monotonic() + 30)
    try:
        await owner.ensure_ready()
        monkeypatch.setattr(owner.provider.implementation.transport, 'call', inference)
        definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
        application = LocalChatApplication(definition, config=config)
        request = LLMRequest(MODEL, CompletionRequest(
            (Message('user', (TextContent('Return Ok.'),)),),
            generation=GenerationOptions(max_output_tokens=8)))
        result = await application.complete(request, llm=owner, context=context)
        assert result.payload.content == (TextContent('Ok'),)
        assert owner.usage[0]['quote']['list_cost_usd'] == pytest.approx(.00000051)
    finally:
        await context.aclose()
        await owner.aclose()
    assert len(acquisitions) == 1
    assert all(not root.exists() for root in temporary_roots)
    assert all(channel.closed and channel.output == [] for channel in private.channels)
    assert not borrowed.cancelled and borrowed.requests == set()
    borrowed.close()


@pytest.fixture(autouse=True)
def unavailable_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    async def acquire(self, request, **kwargs):
        return None, {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)
