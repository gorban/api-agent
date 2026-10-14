"""Pure scoped Compose planning; validation never queries or starts the engine.

Compose's JSON input is also YAML, so no optional YAML parser is required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import urlsplit


class ComposeError(ValueError):
    pass


LOCALSTACK_HOOK_MOUNT = ('${SA_LOCALSTACK_READY_HOOK:?controlled local development value required}'
                        ':/etc/localstack/init/ready.d/10-api-agent-s3.sh:ro,z')


class Scope(Protocol):
    id: str
    project: str

    def labels(self, profile: str) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class ComposePlan:
    profiles: tuple[str, ...]
    services: tuple[str, ...]
    command: tuple[str, ...]
    directory: Path
    environment: Mapping[str, str] = field(repr=False)
    assets: Mapping[str, bytes] = field(repr=False)

    @property
    def enabled(self):
        return bool(self.profiles)

    @property
    def validation_command(self):
        return (*self.command, 'config', '--quiet') if self.enabled else ()


def _endpoint(value):
    if not isinstance(value, str):
        raise ComposeError('Compose endpoint must be a URL string.')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ComposeError('Compose endpoint is invalid.') from error
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ComposeError('Compose endpoint requires an HTTP(S) URL without credentials or fragment.')
    return parsed, port


def _localstack_port(config):
    port = config.get('ALLURE_LOCALSTACK_PORT')
    port = 4566 if port is None else port
    if type(port) is not int or not 1 <= port <= 65535:
        raise ComposeError('ALLURE_LOCALSTACK_PORT must be a port from 1 to 65535.')
    return port


def _local_s3_endpoint(parsed, port, expected=4566):
    return (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}
            and port == expected and not parsed.path.rstrip('/') and not parsed.query)


def selected_profiles(config):
    """Only conventional loopback endpoints request managed local dependencies."""
    mode = config.get('TRACE_MODE', '')
    if not isinstance(mode, str) or mode not in {'', 'LangFuse', 'OTEL'}:
        raise ComposeError('Unsupported TRACE_MODE.')
    profiles = []
    if mode:
        default = 'http://localhost:3000' if mode == 'LangFuse' else 'http://localhost:4318/v1/traces'
        target = config.get('TRACE_TARGET')
        parsed, port = _endpoint(default if target is None else target)
        path = '' if mode == 'LangFuse' else '/v1/traces'
        if (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}
                and port == (3000 if mode == 'LangFuse' else 4318)
                and parsed.path.rstrip('/') == path and not parsed.query):
            profiles.append('langfuse' if mode == 'LangFuse' else 'otel')
    localstack = config.get('ALLURE_S3_USE_LOCALSTACK', False)
    localstack_port = _localstack_port(config)
    if type(localstack) is not bool:
        raise ComposeError('ALLURE_S3_USE_LOCALSTACK must be a boolean.')
    endpoint = config.get('ALLURE_S3_ENDPOINT')
    if localstack and endpoint is None:
        endpoint = f'http://localhost:{localstack_port}'
    if endpoint or localstack:
        parsed, port = _endpoint(endpoint)
        is_local = _local_s3_endpoint(parsed, port, localstack_port)
        if localstack and not is_local:
            raise ComposeError('LocalStack selection requires its loopback S3 endpoint.')
        if is_local:
            profiles.append('localstack')
    return tuple(profiles)


def local_development_environment(scope):
    """Synthetic scope-bound credentials; never read user config or process env.

    Stable values let repeated plans use an existing named-volume database. These
    are public local development placeholders, never production credentials.
    """
    env = {'SA_SCOPE': scope.id, 'COMPOSE_DISABLE_ENV_FILE': '1'}
    for key in ('POSTGRES_PASSWORD', 'CLICKHOUSE_PASSWORD', 'REDIS_PASSWORD', 'MINIO_PASSWORD',
                'SALT', 'NEXTAUTH_SECRET', 'USER_PASSWORD', 'ENCRYPTION_KEY',
                'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
        digest = sha256(f'api-agent-local-development:{scope.id}:{key}'.encode()).hexdigest()
        prefix = 'pk-lf-local-' if key == 'LANGFUSE_PUBLIC_KEY' else 'sk-lf-local-' if key == 'LANGFUSE_SECRET_KEY' else ''
        env['SA_' + key] = prefix + digest
    return env


def validate_document(document, scope, profiles):
    """Reject unscoped labels, exposed dependencies, and incomplete profiles."""
    if set(document) - {'services', 'volumes'}:
        raise ComposeError('Unsupported controlled Compose document feature.')
    for volume in document.get('volumes', {}).values():
        if not isinstance(volume, dict) or set(volume) - {'labels'}:
            raise ComposeError('Controlled Compose named volumes may contain only labels.')
        labels = volume.get('labels', {})
        profile = labels.get('api-agent.profile')
        if profile not in {'langfuse', 'otel', 'localstack'}:
            raise ComposeError('Controlled Compose volume ownership labels are required.')
        expected = dict(scope.labels(profile))
        expected['api-agent.scope'] = '${SA_SCOPE:?controlled local development value required}'
        if labels != expected:
            raise ComposeError('Compose volume ownership labels do not match its scope.')
    services = document.get('services', {})
    if not services:
        raise ComposeError('Compose services are required.')
    active = []
    for name, service in services.items():
        if set(service) - {'command', 'depends_on', 'entrypoint', 'environment',
                'healthcheck', 'image', 'labels', 'ports', 'profiles', 'user', 'volumes'}:
            raise ComposeError('Compose contains an unsafe or unsupported service override.')
        service_profiles = service.get('profiles', [])
        if len(service_profiles) != 1 or service_profiles[0] not in {'langfuse', 'otel', 'localstack'}:
            raise ComposeError('Every service requires exactly one supported profile.')
        profile = service_profiles[0]
        expected = dict(scope.labels(profile))
        expected['api-agent.scope'] = '${SA_SCOPE:?controlled local development value required}'
        if service.get('labels') != expected:
            raise ComposeError('Compose service ownership labels do not match its scope.')
        image = service.get('image', '')
        if ':' not in image.rsplit('/', 1)[-1] or image.endswith(':latest'):
            raise ComposeError('Compose images require a version pin.')
        for port in service.get('ports', []):
            if not isinstance(port, str) or not port.startswith('127.0.0.1:'):
                raise ComposeError('Compose published ports must bind loopback.')
        for dependency in service.get('depends_on', {}):
            if dependency not in services or services[dependency].get('profiles') != service_profiles:
                raise ComposeError('Compose dependency must belong to the same profile.')
        for volume in service.get('volumes', []):
            if name == 'localstack' and volume == LOCALSTACK_HOOK_MOUNT:
                continue
            if isinstance(volume, dict):
                raise ComposeError('Compose bind mounts require the controlled LocalStack hook.')
            if not isinstance(volume, str) or volume.split(':')[0] not in document.get('volumes', {}):
                raise ComposeError('Compose service data requires declared named volumes.')
        if profile in profiles:
            active.append(name)
    return tuple(active)


def default_compose_directory():
    """Locate source checkout assets or packaged assets without consulting CWD."""
    module = Path(__file__).resolve()
    source = module.parents[3] / 'infra' / 'compose'
    if (source / 'compose.json').is_file() and (source / 'empty.env').is_file():
        return source
    return module.parents[1] / 'infra' / 'compose'


ENGINE_ENVIRONMENT_KEYS = frozenset({'PATH', 'DOCKER_HOST', 'CONTAINER_HOST',
    'XDG_RUNTIME_DIR', 'HOME', 'DOCKER_CONFIG', 'API_AGENT_HOST_UID'})


def controlled_engine_environment(environment):
    """Accept only explicit engine transport/config paths, never provider env."""
    result = dict(environment or {})
    if (set(result) - ENGINE_ENVIRONMENT_KEYS or any(not isinstance(value, str)
            or '\x00' in value for value in result.values())):
        raise ComposeError('Unsupported engine environment override.')
    if 'API_AGENT_HOST_UID' in result and not re.fullmatch('[0-9]+', result['API_AGENT_HOST_UID']):
        raise ComposeError('Engine host UID must contain only ASCII digits.')
    return result


def plan_compose(config, scope: Scope, *, compose_directory=None, executable='docker', engine_environment=None):
    """Return reviewable arguments and a complete isolated child environment."""
    if not re.fullmatch('[0-9a-f]{16}', scope.id) or scope.project != 'sa-' + scope.id:
        raise ComposeError('Invalid Compose ownership scope.')
    if not isinstance(executable, str) or not executable or executable.startswith('-'):
        raise ComposeError('Invalid Compose executable.')
    directory = (Path(compose_directory) if compose_directory else default_compose_directory()).resolve()
    profiles = selected_profiles(config)
    try:
        compose_bytes = (directory / 'compose.json').read_bytes()
        document = json.loads(compose_bytes)
        empty_env = directory / 'empty.env'
        empty_bytes = empty_env.read_bytes()
        if any(line.strip() and not line.lstrip().startswith('#') for line in empty_bytes.decode().splitlines()):
            raise ComposeError('Controlled Compose env file must be empty.')
        services = validate_document(document, scope, profiles)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        if isinstance(error, ComposeError):
            raise
        raise ComposeError('Invalid or missing controlled Compose files.') from error
    command = (executable, 'compose', '--project-name', scope.project, '--project-directory', str(directory),
               '-f', str(directory / 'compose.json'), '--env-file', str(empty_env))
    for profile in profiles:
        command += ('--profile', profile)
    environment = {**controlled_engine_environment(engine_environment),
                   **local_development_environment(scope)}
    environment['SA_LOCALSTACK_PORT'] = str(_localstack_port(config))
    hook = directory / 'localstack-ready.sh'
    try:
        if hook.is_symlink() or not hook.is_file():
            raise ComposeError('Controlled LocalStack ready hook is missing or symlinked.')
        environment['SA_LOCALSTACK_READY_HOOK'] = str(hook.resolve())
        hook_bytes = hook.read_bytes()
        environment['SA_LOCALSTACK_READY_DIGEST'] = sha256(hook_bytes).hexdigest()
    except OSError as error:
        raise ComposeError('Cannot read controlled LocalStack ready hook.') from error
    bucket = config.get('ALLURE_S3_BUCKET')
    region = config.get('ALLURE_S3_REGION')
    bucket = 'allure-results' if bucket is None else bucket
    region = 'us-east-1' if region is None else region
    if 'localstack' in profiles:
        if (not isinstance(bucket, str) or not re.fullmatch('[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket)
                or '..' in bucket or re.fullmatch(r'\d+\.\d+\.\d+\.\d+', bucket)):
            raise ComposeError('LocalStack requires a valid selected S3 bucket name.')
        if not isinstance(region, str) or not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)+', region):
            raise ComposeError('LocalStack requires a valid selected region.')
    # Inactive profiles still need controlled interpolation for config validation.
    environment['SA_ALLURE_S3_BUCKET'] = bucket if 'localstack' in profiles else 'allure-results'
    environment['SA_ALLURE_S3_REGION'] = region if 'localstack' in profiles else 'us-east-1'
    if 'langfuse' in profiles:
        for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
            value = config.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or value != value.strip() or '\x00' in value:
                    raise ComposeError('Local Langfuse project keys must be nonempty strings.')
                environment['SA_' + key] = value
    return ComposePlan(profiles, services, command, directory, MappingProxyType(environment),
        MappingProxyType({'compose.json': compose_bytes, 'empty.env': empty_bytes,
                          'localstack-ready.sh': hook_bytes}))


def validate_compose(plan, *, runner=subprocess.run):
    """Run config --quiet only; discard all output to protect resolved secrets."""
    if not plan.enabled:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix='api-agent-compose-') as directory:
            command, environment = snapshot_compose(plan, Path(directory))
            result = runner((*command, 'config', '--quiet'), cwd=directory, env=environment,
                            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ComposeError('Compose config validation unavailable.') from error
    if result.returncode:
        raise ComposeError(f'Compose config validation failed (exit {result.returncode}).')
    return True


def snapshot_compose(plan, directory):
    """Materialize only reviewed bytes; source assets are never reread.

    Managers retain the directory while containers may use bind-mounted hooks. Config
    validation can use a temporary directory because it creates no services.
    """
    for name, content in plan.assets.items():
        path = directory / name
        path.write_bytes(content)
        path.chmod(0o755 if name == 'localstack-ready.sh' else 0o444)
    command = tuple(str(directory / Path(arg).name) if arg in {
        str(plan.directory / 'compose.json'), str(plan.directory / 'empty.env')} else arg
        for arg in plan.command)
    command = tuple(str(directory) if i and command[i - 1] == '--project-directory' else arg
                    for i, arg in enumerate(command))
    environment = dict(plan.environment)
    environment['SA_LOCALSTACK_READY_HOOK'] = str(directory / 'localstack-ready.sh')
    return command, environment
