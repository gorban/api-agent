"""Selected native acquisition crosses the real worker and synthetic CLI offline."""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock.ambient import AmbientSession
from api_agent.providers.contracts import ProviderAccessError
from api_agent.runtime.local_llm import LocalLLM


ROOT = Path(__file__).resolve().parents[2]


class HandoffChannel:
    def __init__(self):
        self.closed = False
        self.output = []
        self.replied = False

    def write(self, text):
        self.output.append(text)

    def read(self, timeout):
        if self.output and not self.replied:
            self.replied = True
            return SimpleNamespace(action='input', value='syntheticconfirmation')
        time.sleep(0.01)
        return None

    def close(self):
        self.closed = True


class HandoffOwner:
    def __init__(self):
        self.channel = HandoffChannel()

    def open(self, label):
        assert label == 'AWS sign-in'
        return self.channel


@pytest.fixture
def native_launch(monkeypatch, tmp_path):
    home = tmp_path / 'user-home'
    (home / '.aws').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('AWS_CONFIG_FILE', str(home / '.aws/config'))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(home / '.aws/credentials'))
    monkeypatch.setenv('AWS_LOGIN_CACHE_DIRECTORY', str(home / '.aws/login/cache'))
    trace = tmp_path / 'trace.jsonl'
    executable = tmp_path / 'synthetic-cli'
    executable.write_text(f'#!{sys.executable}\n' + '''
import hashlib, json, os, sys
from pathlib import Path
def record(stage):
    with open(os.environ['SYNTHETIC_TRACE'], 'a') as stream:
        stream.write(json.dumps(stage) + '\\n')
if '--version' in sys.argv:
    record('cli-version')
    print('aws-cli/2.32.0')
elif 'login' in sys.argv:
    record('cli-login')
    print('Complete synthetic handoff', flush=True)
    assert sys.stdin.readline().strip() == 'syntheticconfirmation'
    config = Path(os.environ['AWS_CONFIG_FILE'])
    with config.open('a') as stream:
        stream.write('login_session = synthetic-session\\n')
    cache = Path(os.environ['AWS_LOGIN_CACHE_DIRECTORY'])
    cache.mkdir(parents=True, exist_ok=True)
    (cache / (hashlib.sha256(b'synthetic-session').hexdigest() + '.json')).write_text('{\"synthetic\":\"refresh-token\"}')
    record('cli-login-completed')
else:
    record('cli-export')
    if os.environ['SYNTHETIC_CASE'] == 'export':
        sys.exit(1)
    print(json.dumps(dict(Version=1, AccessKeyId='synthetic-key',
        SecretAccessKey='synthetic-secret', SessionToken='synthetic-token',
        Expiration='2999-01-01T00:00:00Z')))
''')
    executable.chmod(0o700)
    bootstrap = tmp_path / 'synthetic_worker.py'
    bootstrap.write_text('''
import json, os, sys
from types import SimpleNamespace
from api_agent.llm_providers.bedrock import login_worker as worker
def record(stage):
    with open(os.environ['SYNTHETIC_TRACE'], 'a') as stream:
        stream.write(json.dumps(stage) + '\\n')
worker.shutil.which = lambda name: os.environ['SYNTHETIC_CLI']
emit = worker.emit
def observed_emit(frame):
    if frame == {'auth_done': True}:
        record('worker-auth-done')
    if 'result' in frame:
        record('worker-result')
    emit(frame)
worker.emit = observed_emit
def client(service, **options):
    assert service == 'sts'
    assert options['aws_access_key_id'] == 'synthetic-key'
    assert options['aws_secret_access_key'] == 'synthetic-secret'
    assert options['aws_session_token'] == 'synthetic-token'
    record('sts-client')
    def identity():
        record('sts-identity')
        account = '999999999999' if os.environ['SYNTHETIC_CASE'] == 'identity' else '123456789012'
        return dict(Account=account, Arn=f'arn:aws:iam::{account}:user/example')
    return SimpleNamespace(get_caller_identity=identity, close=lambda: record('sts-close'))
sys.modules['boto3'] = SimpleNamespace(client=client)
worker.main()
''')
    launches = []
    real_launch = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        assert Path(args[1]).name == 'login_worker.py'
        environment = kwargs['env']
        environment.update(SYNTHETIC_TRACE=str(trace), SYNTHETIC_CLI=str(executable),
                           SYNTHETIC_CASE=case[0], PYTHONPATH=str(ROOT / 'src'))
        process = await real_launch(args[0], str(bootstrap), **kwargs)
        launches.append(process)
        return process

    async def unavailable(self, request, **kwargs):
        return None, {}

    case = ['success']
    monkeypatch.setattr(AmbientSession, 'acquire', unavailable)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    return case, trace, launches


def configuration():
    return {
        'LLM_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
        'BEDROCK_SESSION_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/bedrock/aws_login.py') + ':factory',
        'BEDROCK_AUTH_MODE': 'session', 'BEDROCK_API_MODE': 'responses',
        'BEDROCK_PROFILE': 'default', 'BEDROCK_REGION': 'us-east-1',
        'NO_STD_IN': False,
        'BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID': '123456789012',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('case, stage, kind, reason', [
    ('success', None, None, None),
    ('identity', 'identity_verification', 'invalid_request', 'malformed'),
    ('export', 'credential_export', 'unknown', 'unknown'),
])
async def test_selected_native_setup_preserves_post_login_outcome(native_launch, case, stage, kind, reason):
    selected_case, trace, launches = native_launch
    selected_case[0] = case
    interaction = HandoffOwner()
    owner = LocalLLM(configuration(), ROOT, interaction_owner=interaction)
    try:
        if case == 'success':
            await owner.ensure_ready()
        else:
            with pytest.raises(ProviderAccessError) as failure:
                await owner.ensure_ready()
            assert failure.value.kind == kind
            assert failure.value.code == reason
            assert failure.value.execution == 'not_started'
            assert failure.value.message == (
                f'AWS sign-in failed. Stage: {stage}; category: {kind}; reason: {reason}.')
    finally:
        await owner.aclose()

    stages = [json.loads(line) for line in trace.read_text().splitlines()]
    assert stages[:5] == ['cli-version', 'cli-login', 'cli-login-completed',
                          'worker-auth-done', 'cli-export']
    assert stages[5:] == ([] if case == 'export' else
                         ['sts-client', 'sts-identity', 'sts-close'] +
                         (['worker-result'] if case == 'success' else []))
    assert interaction.channel.replied and interaction.channel.closed
    assert interaction.channel.output == ['Complete synthetic handoff\n']
    assert len(launches) == 1 and launches[0].returncode is not None
