"""Synthetic captured-build and credential-file checks."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime.tool_preparation import (
    BuildRequest, PreparationError, SecretInput, prepare_image,
)

IMAGE_ID = 'sha256:' + 'a' * 64


def request(**changes):
    values = dict(local_tag='sample/tool:prepared', expected_image_id=IMAGE_ID,
                  context={'src/main.txt': b'hello'}, dockerfile=b'FROM scratch')
    values.update(changes)
    return BuildRequest(**values)


@pytest.mark.parametrize('identifier,content', [('', b'x'), ('A', b'x'), (3, b'x'),
    ('valid', ''), ('valid', b''), ('valid', b'x' * 65537)])
def test_secret_admission(identifier, content):
    with pytest.raises(PreparationError):
        SecretInput(identifier, content)


@pytest.mark.parametrize('change', [
    {'local_tag': None}, {'local_tag': 'sample'}, {'expected_image_id': None},
    {'expected_image_id': 'sha256:bad'}, {'dockerfile': ''}, {'dockerfile': b''},
    {'dockerfile': b'x' * 262145}, {'timeout': True}, {'timeout': float('nan')},
    {'timeout': 0}, {'timeout': 601}, {'secrets': []},
    {'secrets': tuple(SecretInput('s' + str(i), b'x') for i in range(9))},
    {'secrets': (None,)}, {'secrets': (SecretInput('a', b'x'), SecretInput('a', b'y'))},
    {'context': []}, {'context': {}}, {'context': {str(i): b'' for i in range(513)}},
    {'context': {1: b''}}, {'context': {'x' * 241: b''}}, {'context': {'': b''}},
    {'context': {'/a': b''}}, {'context': {'a\\b': b''}}, {'context': {'../a': b''}},
    {'context': {'./a': b''}}, {'context': {'a//b': b''}}, {'context': {'a/': b''}}, {'context': {'a\x00': b''}}, {'context': {'a': ''}},
    {'context': {'a': b'x' * (8 * 1024 * 1024 + 1)}},
    {'context': {str(i): b'x' * (7 * 1024 * 1024) for i in range(5)}},
    {'context': {'a': b'', 'a/b': b''}},
])
def test_request_admission(change):
    with pytest.raises(PreparationError):
        request(**change)


def test_context_capture_and_digest():
    source = {'b': b'world', 'a': b'hello'}
    captured = request(context=source)
    source['a'] = b'changed'
    assert captured.context['a'] == b'hello'
    assert captured.context_digest == request(context={'a': b'hello', 'b': b'world'}).context_digest
    assert captured.context_digest != request(context={'a': b'changed', 'b': b'world'}).context_digest
    assert captured.context_digest != request(context={'a': b'hello', 'b': b'world'}, dockerfile=b'FROM other').context_digest
    with pytest.raises(TypeError):
        captured.context['c'] = b'x'
    assert request(context=captured.context).context == captured.context


def test_build_uses_bound_engine_and_secret_files():
    calls, roots, verified = [], [], []
    target = SimpleNamespace(executable='/engine/cli', environment={'ONLY': 'captured'},
                             verify=lambda **kwargs: verified.append(kwargs))
    secret = SecretInput('credential', b'synthetic-sensitive-value')
    build = request(secrets=(secret,))

    def run(command, **options):
        calls.append(command)
        root = options['directory']
        roots.append(root)
        assert options['environment'] == {'ONLY': 'captured'}
        assert options['output_limit'] == 65536
        assert options['deadline'] > 0
        assert secret.content.decode() not in repr(command)
        assert secret.content.decode() not in options['output_scrubber'](secret.content.decode())
        if command[1] == 'build':
            assert (root / 'context/src/main.txt').read_bytes() == b'hello'
            assert (root / 'Dockerfile').read_bytes() == b'FROM scratch'
            secret_path = root / 'secret-0'
            assert secret_path.read_bytes() == secret.content
            assert secret_path.stat().st_mode & 0o777 == 0o600
            assert not (root / 'context/secret-0').exists()
            assert command[command.index('--secret') + 1] == 'id=credential,src=' + str(secret_path)
        return IMAGE_ID

    receipt = prepare_image(build, target=target, runner=run)
    assert receipt.image_id == IMAGE_ID
    assert receipt.local_tag == build.local_tag
    assert receipt.context_digest == build.context_digest
    assert len(verified) == 2
    assert calls[1] == ('/engine/cli', 'image', 'inspect', '--format', '{{.Id}}', build.local_tag)
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize('failure', ['build', 'inspect', 'verify', 'mismatch'])
def test_failure_discards_output_and_cleans_files(failure):
    roots = []
    def verify(**kwargs):
        if failure == 'verify':
            raise RuntimeError('synthetic-sensitive-value')
    target = SimpleNamespace(executable='/engine/cli', environment={}, verify=verify)
    def run(command, **options):
        roots.append(options['directory'])
        if command[1] == failure or (command[1] == 'image' and failure == 'inspect'):
            raise RuntimeError('synthetic-sensitive-value')
        return 'different'
    with pytest.raises(PreparationError, match='Tool image preparation failed') as error:
        prepare_image(request(), target=target, runner=run)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert 'synthetic-sensitive-value' not in str(error.value)
    assert all(not root.exists() for root in roots)


def test_wrong_request_does_not_touch_engine():
    with pytest.raises(PreparationError, match='Invalid image'):
        prepare_image(None, target=None)


@pytest.mark.parametrize('profile,value,selected', [
    ('local', 'http://example.test', True),
    ('deployment', 'http://127.0.0.1:8000', True),
    ('deployment', None, False)])
def test_external_deployment_unused_never_resolve_build_secrets_or_engine(profile, value, selected):
    from api_agent.runtime.tool_policy import sidecar_policy
    from api_agent.runtime.tool_preparation import prepare_selected_sidecar
    policy = sidecar_policy(value, source='environment', profile=profile, selected=selected)
    def forbidden():
        pytest.fail('unselected preparation effect')
    assert prepare_selected_sidecar(policy, request_factory=forbidden,
                                    target_factory=forbidden, runner=forbidden) is None


def test_managed_policy_resolves_captured_request_once():
    from types import SimpleNamespace
    from api_agent.runtime.tool_policy import sidecar_policy, SidecarPolicy
    from api_agent.runtime.tool_preparation import prepare_selected_sidecar
    events = []
    target = SimpleNamespace(executable='/engine/cli', environment={}, verify=lambda **kw: None)
    def request_factory():
        events.append('request')
        return request()
    def target_factory():
        events.append('target')
        return target
    receipt = prepare_selected_sidecar(sidecar_policy('', source='default'),
        request_factory=request_factory, target_factory=target_factory,
        runner=lambda *a, **kw: IMAGE_ID)
    assert receipt.image_id == IMAGE_ID
    assert events == ['request', 'target']
    with pytest.raises(PreparationError):
        prepare_selected_sidecar(None, request_factory=request_factory, target_factory=target_factory)
    with pytest.raises(PreparationError):
        prepare_selected_sidecar(SidecarPolicy('invalid', '', 'default'),
            request_factory=request_factory, target_factory=target_factory)


def test_noncanonical_context_paths_rejected():
    with pytest.raises(PreparationError):
        request(context={'a//b': b'content'})


@pytest.mark.parametrize('failing_factory', ['request', 'target'])
def test_managed_factory_failures_do_not_expose_paths_or_secrets(failing_factory):
    from types import SimpleNamespace
    from api_agent.runtime.tool_policy import sidecar_policy
    from api_agent.runtime.tool_preparation import prepare_selected_sidecar
    def failed():
        raise OSError('sensitive synthetic owner path')
    with pytest.raises(PreparationError, match='Tool image preparation failed') as error:
        prepare_selected_sidecar(sidecar_policy('', source='default'),
            request_factory=failed if failing_factory == 'request' else request,
            target_factory=failed if failing_factory == 'target' else lambda: None)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert 'sensitive' not in str(error.value)
