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
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import urlsplit


class ComposeError(ValueError):
    pass


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
    endpoint = config.get('ALLURE_S3_ENDPOINT')
    if endpoint:
        parsed, port = _endpoint(endpoint)
        if (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}
                and port == 4566 and not parsed.path.rstrip('/') and not parsed.query):
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
    services = document.get('services', {})
    if not services:
        raise ComposeError('Compose services are required.')
    active = []
    for name, service in services.items():
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
        if 'container_name' in service or service.get('network_mode') or service.get('privileged') or service.get('env_file'):
            raise ComposeError('Compose contains an unsafe service override.')
        for port in service.get('ports', []):
            if not isinstance(port, str) or not port.startswith('127.0.0.1:'):
                raise ComposeError('Compose published ports must bind loopback.')
        for dependency in service.get('depends_on', {}):
            if dependency not in services or services[dependency].get('profiles') != service_profiles:
                raise ComposeError('Compose dependency must belong to the same profile.')
        for volume in service.get('volumes', []):
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


def plan_compose(config, scope: Scope, *, compose_directory=None, executable='docker'):
    """Return reviewable arguments and a complete isolated child environment."""
    if not re.fullmatch('[0-9a-f]{16}', scope.id) or scope.project != 'sa-' + scope.id:
        raise ComposeError('Invalid Compose ownership scope.')
    if not isinstance(executable, str) or not executable or executable.startswith('-'):
        raise ComposeError('Invalid Compose executable.')
    directory = (Path(compose_directory) if compose_directory else default_compose_directory()).resolve()
    profiles = selected_profiles(config)
    try:
        document = json.loads((directory / 'compose.json').read_text())
        empty_env = directory / 'empty.env'
        if any(line.strip() and not line.lstrip().startswith('#') for line in empty_env.read_text().splitlines()):
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
    return ComposePlan(profiles, services, command, directory,
                       MappingProxyType(local_development_environment(scope)))


def validate_compose(plan, *, runner=subprocess.run):
    """Run config --quiet only; discard all output to protect resolved secrets."""
    if not plan.enabled:
        return False
    try:
        result = runner(plan.validation_command, cwd=plan.directory, env=dict(plan.environment),
                        capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ComposeError('Compose config validation unavailable.') from error
    if result.returncode:
        raise ComposeError(f'Compose config validation failed (exit {result.returncode}).')
    return True
