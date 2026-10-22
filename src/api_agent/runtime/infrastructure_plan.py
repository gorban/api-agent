"""Pure admission of captured, selected Compose infrastructure declarations."""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from .compose import ComposeError, ComposePlan, plan_compose
from .infrastructure_contract import InfrastructureService, Readiness


@dataclass(frozen=True)
class ResolvedCheck:
    kind: str
    url: str = ''
    status: int = 200
    identity_field: str = ''
    identity_value: str = ''
    timeout: int = 30


@dataclass(frozen=True)
class ResolvedEndpoint:
    name: str
    host: str
    in_project: str


@dataclass(frozen=True)
class PlannedService:
    id: str
    compose_name: str
    profile: str
    dependencies: tuple[str, ...]
    readiness: ResolvedCheck
    endpoints: tuple[ResolvedEndpoint, ...]
    seed_completion: ResolvedCheck | None
    asset_digests: Mapping[str, str]
    asset_paths: Mapping[str, str]


def _name(prefix: str, identifier: str) -> str:
    return prefix + sha256(identifier.encode('ascii')).hexdigest()[:20]


def _url(value: str, *, local: bool, hostname: str | None = None):
    if type(value) is not str or len(value) > 512:
        raise ComposeError('Infrastructure endpoint is invalid.')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ComposeError('Infrastructure endpoint is invalid.') from error
    if (parsed.scheme != 'http' or not parsed.hostname or port is None
            or parsed.username or parsed.password or parsed.fragment or parsed.query
            or parsed.path not in ('', '/')
            or (local and parsed.hostname not in {'127.0.0.1', 'localhost'})
            or (hostname is not None and parsed.hostname not in {'self', hostname})):
        raise ComposeError('Infrastructure endpoint must use an admitted HTTP host and port.')
    return parsed, port


def _check(rule: Readiness, endpoints: Mapping[str, ResolvedEndpoint]) -> ResolvedCheck:
    if rule.kind == 'health':
        return ResolvedCheck('health', status=rule.status, timeout=rule.timeout)
    if rule.kind != 'http' or '/' not in rule.target:
        raise ComposeError('Infrastructure HTTP readiness requires an endpoint and path.')
    name, path = rule.target.split('/', 1)
    if (name not in endpoints or not path or any(part in ('', '.', '..') for part in path.split('/'))
            or '?' in path or '#' in path or '\\' in path):
        raise ComposeError('Infrastructure HTTP readiness target is invalid.')
    return ResolvedCheck('http', endpoints[name].host.rstrip('/') + '/' + path,
                         rule.status, rule.identity_field, rule.identity_value, rule.timeout)


def _activation(service: InfrastructureService, values: Mapping, schema) -> bool:
    predicate = service.activation
    if predicate is None:
        return True
    declaration = schema.fields.get(predicate.field)
    if declaration is None or declaration.type not in {'bool', 'int', 'str'}:
        raise ComposeError('Infrastructure activation references an unsupported field.')
    expected = {'bool': bool, 'int': int, 'str': str}[declaration.type]
    if type(predicate.equals) is not expected:
        raise ComposeError('Infrastructure activation type differs from field declaration.')
    actual = values.get(predicate.field)
    if actual is not None and type(actual) is not expected:
        raise ComposeError('Infrastructure activation requires fresh typed configuration.')
    return actual == predicate.equals and type(actual) is expected


def _verify_capture(service: InfrastructureService) -> None:
    for item in (service.declaration_source, service.compose_source, *service.assets):
        if (type(item.body) is not bytes or type(item.sha256) is not str
                or sha256(item.body).hexdigest() != item.sha256
                or type(item.relative_path) is not str or not item.relative_path
                or item.relative_path.startswith('/') or '\\' in item.relative_path
                or any(part in ('', '.', '..') for part in item.relative_path.split('/'))):
            raise ComposeError('Infrastructure captured bytes do not match their digest.')


def _unix_transport(environment: Mapping[str, str]) -> None:
    endpoints = [environment[key] for key in ('DOCKER_HOST', 'CONTAINER_HOST') if key in environment]
    if not endpoints or len(set(endpoints)) != 1:
        raise ComposeError('Conditional infrastructure requires one explicit Unix engine transport.')
    value = endpoints[0]
    parsed = urlsplit(value)
    if (parsed.scheme != 'unix' or not parsed.path.startswith('/') or parsed.netloc
            or parsed.query or parsed.fragment or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise ComposeError('Conditional infrastructure requires one explicit Unix engine transport.')


def _compose_fragment(service, name, profile, scope, all_profiles, assets, used_ports, dependency_outputs):
    source = service.compose
    allowed = {'image', 'ports', 'environment', 'volumes', 'command'}
    if set(source) - allowed or 'image' not in source:
        raise ComposeError('Unsupported infrastructure Compose service feature.')
    image = source['image']
    if (type(image) is not str or len(image) > 256 or '@sha256:' not in image
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]+', image)
            or not re.fullmatch('[0-9a-f]{64}', image.rsplit('@sha256:', 1)[-1])):
        raise ComposeError('Infrastructure image requires an immutable SHA256 digest.')
    result = {'image': image, 'profiles': [profile], 'labels': {
        **scope.labels(profile, profiles=all_profiles),
        'api-agent.scope': '${SA_SCOPE:?controlled local development value required}',
        'api-agent.declaration-sha256': service.declaration_source.sha256,
        'api-agent.compose-source-sha256': service.compose_source.sha256}}
    ports = source.get('ports', ())
    if not isinstance(ports, tuple) or len(ports) > 16:
        raise ComposeError('Infrastructure ports must be a bounded list.')
    admitted_ports = []
    for item in ports:
        if type(item) is not str or not re.fullmatch(r'127\.0\.0\.1:([1-9][0-9]{0,4}):([1-9][0-9]{0,4})', item):
            raise ComposeError('Infrastructure ports must bind numbered loopback ports.')
        host, container = map(int, item.split(':')[1:])
        if max(host, container) > 65535 or host in used_ports:
            raise ComposeError('Infrastructure published port collides or is invalid.')
        used_ports.add(host)
        admitted_ports.append(item)
    if admitted_ports:
        result['ports'] = admitted_ports
    environment = source.get('environment', {})
    if (not isinstance(environment, Mapping) or len(environment) > 32
            or any(type(key) is not str or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}', key)
                   for key in environment)):
        raise ComposeError('Unsupported infrastructure environment declaration.')
    if environment:
        resolved_environment = {}
        for key, value in environment.items():
            if type(value) is str and len(value) <= 512 and '$' not in value and '\x00' not in value:
                resolved_environment[key] = value
            elif isinstance(value, Mapping) and set(value) == {'endpoint', 'address'}:
                reference = value['endpoint']
                if (type(reference) is not str or value['address'] != 'in_project'
                        or reference not in dependency_outputs):
                    raise ComposeError('Infrastructure endpoint reference requires an explicit dependency output.')
                resolved_environment[key] = dependency_outputs[reference]
            else:
                raise ComposeError('Unsupported infrastructure environment declaration.')
        result['environment'] = resolved_environment
    volumes = source.get('volumes', ())
    if not isinstance(volumes, tuple) or len(volumes) > 32:
        raise ComposeError('Infrastructure volumes must be a bounded list.')
    mounted = set()
    digest_map, path_map, entries, named = {}, {}, [], set()
    captured = {item.relative_path: item for item in service.assets}
    for item in volumes:
        if not isinstance(item, Mapping) or set(item) not in ({'asset', 'target'}, {'volume', 'target'}):
            raise ComposeError('Infrastructure volumes require typed captured assets or named volumes.')
        target = item['target']
        if (type(target) is not str or not target.startswith('/') or ':' in target or '$' in target or '\\' in target
                or any(part in ('', '.', '..') for part in target[1:].split('/')) or target in mounted):
            raise ComposeError('Infrastructure volume target is invalid or duplicated.')
        mounted.add(target)
        if 'asset' in item:
            if type(item['asset']) is not str:
                raise ComposeError('Infrastructure mount asset name is invalid.')
            asset = captured.get(item['asset'])
            if asset is None:
                raise ComposeError('Infrastructure mount must refer to a captured asset.')
            key = 'SA_INFRA_ASSET_' + _name('', service.id + ':' + asset.relative_path).upper()
            path = 'conditional/' + _name('', service.id) + '/' + asset.relative_path
            if path in assets:
                raise ComposeError('Infrastructure asset snapshot collision.')
            assets[path] = asset.body
            digest_map[key], path_map[key] = asset.sha256, path
            entries.append('${' + key + ':?controlled asset required}:' + target + ':ro,z')
        else:
            volume = item['volume']
            if type(volume) is not str or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', volume):
                raise ComposeError('Infrastructure named volume is invalid.')
            qualified = _name('infra-', service.id + ':' + volume)
            named.add(qualified)
            entries.append(qualified + ':' + target)
    if entries:
        result['volumes'] = entries
    command = source.get('command')
    if command is not None:
        if (not isinstance(command, tuple) or len(command) != 2
                or command[0] not in {'python', 'python3', '/usr/local/bin/python3'}
                or type(command[1]) is not str
                or command[1] not in {item['target'] for item in volumes
                    if isinstance(item, Mapping) and 'asset' in item}):
            raise ComposeError('Infrastructure command must run a captured read-only Python asset directly.')
        result['command'] = list(command)
    if service.seed is not None and service.seed.asset not in {
            item['asset'] for item in volumes if isinstance(item, Mapping) and 'asset' in item}:
        raise ComposeError('Seed asset must be mounted read-only.')
    return result, digest_map, path_map, named, admitted_ports


def plan_infrastructure(config, schema, contributions, scope, *, compose_directory=None,
                        executable='docker', engine_environment=None) -> ComposePlan:
    """Merge active captured services into an immutable, side-effect-free Compose plan."""
    values = config.values if not isinstance(config, Mapping) and hasattr(config, 'values') else config
    if not isinstance(values, Mapping) or not hasattr(schema, 'fields'):
        raise ComposeError('Infrastructure planning requires typed configuration and schema.')
    if hasattr(config, 'errors') and (config.errors or config.help_requested):
        raise ComposeError('Infrastructure planning requires a completed configuration.')
    base = plan_compose(values, scope, compose_directory=compose_directory,
                        executable=executable, engine_environment=engine_environment)
    if not contributions:
        return base
    by_id = {}
    for item in contributions:
        if not isinstance(item, InfrastructureService) or item.id in by_id:
            raise ComposeError('Infrastructure contributions require unique captured services.')
        _verify_capture(item)
        by_id[item.id] = item
    active = {key: item for key, item in by_id.items() if _activation(item, values, schema)}
    if not active:
        return base
    _unix_transport(base.environment)
    for item in active.values():
        if any(dependency not in active or dependency == item.id for dependency in item.dependencies):
            raise ComposeError('Active infrastructure dependency is unavailable or self-referential.')
    ordered, visiting, visited = [], set(), set()
    def visit(key):
        if key in visiting:
            raise ComposeError('Infrastructure dependency cycle.')
        if key not in visited:
            visiting.add(key)
            for dependency in active[key].dependencies:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)
            ordered.append(key)
    for key in sorted(active):
        visit(key)
    names = {key: _name('infra-', key) for key in active}
    profiles = {key: _name('infra-', item.owner) for key, item in active.items()}
    if len(set(names.values())) != len(names):
        raise ComposeError('Infrastructure Compose name collision.')
    all_profiles = frozenset((*base.profiles, *profiles.values(), 'langfuse', 'otel', 'localstack'))
    document = json.loads(base.assets['compose.json'])
    assets = dict(base.assets)
    environment = dict(base.environment)
    used_ports = set()
    for spec in document['services'].values():
        if spec.get('profiles', [None])[0] not in base.profiles:
            continue
        for port in spec.get('ports', ()):
            value = port.split(':')[1]
            if value.isdecimal():
                used_ports.add(int(value))
            elif 'SA_JAEGER_VIEWER_PORT' in value:
                used_ports.add(int(base.environment['SA_JAEGER_VIEWER_PORT']))
            else:
                used_ports.add(int(base.environment['SA_LOCALSTACK_PORT']))
    planned = []
    endpoints_seen = set()
    named_volumes = set(document.get('volumes', {}))
    outputs_by_service = {}
    for key in ordered:
        item, name, profile = active[key], names[key], profiles[key]
        dependency_outputs = {f'{dependency}/{output.name}': output.in_project
            for dependency in item.dependencies for output in outputs_by_service[dependency]}
        fragment, digests, paths, declared_volumes, ports = _compose_fragment(
            item, name, profile, scope, all_profiles, assets, used_ports, dependency_outputs)
        outputs = []
        for output in item.endpoints:
            host, host_port = _url(output.host, local=True)
            project, container_port = _url(output.in_project, local=False, hostname=name)
            if ((host_port, container_port) not in {
                    (int(port.split(':')[1]), int(port.split(':')[2])) for port in ports}
                    or host.scheme != project.scheme or (item.id, output.name) in endpoints_seen):
                raise ComposeError('Infrastructure endpoint does not match its published port.')
            endpoints_seen.add((item.id, output.name))
            internal = f'http://{name}:{container_port}'
            outputs.append(ResolvedEndpoint(output.name, output.host, internal))
        endpoint_map = {item.name: item for item in outputs}
        outputs_by_service[key] = tuple(outputs)
        readiness = _check(item.readiness, endpoint_map)
        completion = _check(item.seed.completion, endpoint_map) if item.seed else None
        fragment['depends_on'] = {names[dependency]: {'condition': 'service_started'}
                                  for dependency in item.dependencies}
        document['services'][name] = fragment
        for volume in declared_volumes:
            if volume in named_volumes:
                raise ComposeError('Infrastructure named volume collision.')
            named_volumes.add(volume)
            document.setdefault('volumes', {})[volume] = {'labels': {
                **scope.labels(profile, profiles=all_profiles),
                'api-agent.scope': '${SA_SCOPE:?controlled local development value required}'}}
        planned.append(PlannedService(item.id, name, profile,
            tuple(names[dependency] for dependency in item.dependencies), readiness,
            tuple(outputs), completion, MappingProxyType(digests), MappingProxyType(paths)))
        environment.update({key: 'sha256:' + value for key, value in digests.items()})
    assets['compose.json'] = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
    selected_profiles = tuple(dict.fromkeys((*base.profiles, *(profiles[key] for key in ordered))))
    command = base.command + tuple(part for profile in selected_profiles[len(base.profiles):]
                                    for part in ('--profile', profile))
    return replace(base, profiles=selected_profiles, services=base.services + tuple(names[key] for key in ordered),
                   command=command, environment=MappingProxyType(environment),
                   assets=MappingProxyType(assets), conditional=tuple(planned))
