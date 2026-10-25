"""Join selected tool dependencies to existing infrastructure and service owners."""
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def capture_prerequisites(tools, roots):
    """Capture only infrastructure owners explicitly declared by selected tools."""
    from api_agent.runtime.infrastructure_contract import (load_infrastructure_contributions,
        _capture, MAX_DECLARATION_BYTES)
    owners, assets, contributions = {}, {}, {}
    for tool in tools:
        declaration = tool.infrastructure
        if not declaration:
            if tool.pending or tool.prerequisite_roots:
                raise ValueError('Selected tool prerequisites have no declared owner')
            continue
        root = Path(declaration['owner_root']).absolute()
        if root.resolve(strict=True) != root or not any(root.is_relative_to(Path(allowed).absolute()) for allowed in roots.values()):
            raise ValueError('Tool infrastructure root is outside admitted roots')
        owner = declaration['owner']
        known = owners.setdefault(owner, (root, set()))
        if known[0] != root:
            raise ValueError('Tool infrastructure owner has conflicting roots')
        known[1].update(declaration['declarations'])
    for owner, (root, declarations) in sorted(owners.items()):
        context = SimpleNamespace(pack_root=root, pack_id=owner,
                                  infrastructure_paths=tuple(sorted(declarations)))
        for service in load_infrastructure_contributions(context):
            if service.id in contributions:
                raise ValueError('Duplicate tool infrastructure identity')
            contributions[service.id] = service
            for asset in (service.declaration_source, service.compose_source, *service.assets):
                path = root / asset.relative_path
                if path in assets and assets[path] != asset.body:
                    raise ValueError('Tool infrastructure changed during admission')
                assets[path] = asset.body
        for declaration in declarations:
            declaration = Path(declaration)
            if declaration not in assets:
                asset = _capture(root, declaration.relative_to(root).as_posix(), MAX_DECLARATION_BYTES)
                assets[declaration] = asset.body
    return tuple(contributions.values()), MappingProxyType(assets)


def selected_contributions(plan):
    """Let the existing planner own ordering; select its required root closure."""
    available = {item.id: item for item in plan.infrastructure_contributions}
    required, pending = set(), []
    for tool in plan.tools:
        pending.extend(tool.prerequisite_roots)
        pending.extend(binding['output'].rsplit('/', 1)[0] for binding in tool.pending.values())
    while pending:
        identity = pending.pop()
        if identity in required:
            continue
        if identity not in available:
            raise ValueError('Selected tool prerequisite is unavailable')
        required.add(identity)
        pending.extend(available[identity].dependencies)
    if getattr(plan, 'config', None) is not None:
        from api_agent.runtime.infrastructure_plan import _activation
        if any(not _activation(available[identity], plan.config.values, plan.schema) for identity in required):
            raise ValueError('Required selected tool infrastructure is disabled')
    return tuple(available[identity] for identity in sorted(required))


def bind_tool_outputs(plan, infrastructure, *, address='host'):
    """Copy ready outputs into local values without replacing saved user input."""
    if address not in ('host', 'in_project'):
        raise ValueError('Invalid tool output address scope')
    tools = []
    shared = getattr(plan, 'shared_primary', {})
    by_id = {}
    waiting = list(plan.tools)
    while waiting:
        progress = False
        for tool in tuple(waiting):
            primary_id = shared.get(tool.id)
            if primary_id and primary_id not in by_id:
                continue
            values = _plain(tool.local_values)
            for slot, binding in tool.pending.items():
                service_id, output = binding['output'].rsplit('/', 1)
                matches = [getattr(item, address) for item in infrastructure.endpoints.get(service_id, ())
                           if item.name == output]
                if len(matches) != 1 or not matches[0]:
                    raise ValueError('Selected tool output is not ready')
                values[slot] = matches[0].rstrip('/') + binding.get('path', '')
            if primary_id:
                primary = by_id[primary_id]
                for slot in ('ENDPOINT', 'HEADERS'):
                    if slot in primary.local_values:
                        values[slot] = _plain(primary.local_values[slot])
            bound = replace(tool, local_values=MappingProxyType(values), pending=MappingProxyType({}))
            by_id[tool.id] = bound
            tools.append(bound)
            waiting.remove(tool)
            progress = True
        if not progress:
            raise ValueError('Cyclic shared tool output binding')
    return replace(plan, tools=tuple(by_id[tool.id] for tool in plan.tools))



def prepare_tool_plan(plan):
    """Pin every selected executable receipt once before trusted dispatch."""
    from api_agent.tooling.admission import _check_admission, prepare_tool_admission
    admitted, tools = {}, []
    for tool in plan.tools:
        admission = tool.admission
        if admission is None:
            raise ValueError('Selected tool executable admission is missing')
        _check_admission(admission)
        key = (admission.identity, admission.entry, admission.factory, admission.interface)
        if key not in admitted:
            admitted[key] = prepare_tool_admission(admission)
        tools.append(replace(tool, admission=admitted[key]))
    return replace(plan, tools=tuple(tools))

def tool_configurations(plan, services):
    """Validate every tool input/service before the first factory is imported."""
    configurations = []
    identities = [tool.id for tool in plan.tools]
    if len(identities) != len(set(identities)) or any(identity not in identities for identity in plan.bindings.values()):
        raise ValueError('Selected tool identities or graph bindings conflict')
    for tool in plan.tools:
        if tool.interface != 'verified-read@1' or tool.admission is None or tool.pending:
            raise ValueError('Selected tool admission or ready outputs are missing')
    plan = prepare_tool_plan(plan)
    for tool in plan.tools:
        local_services = {key: value for key, value in services.items() if key != 'service_registry'}
        if tool.service_bindings or tool.service_capabilities:
            registry = services.get('service_registry')
            if registry is None:
                raise ValueError('Required operation service registry is unavailable')
            local_services['tool_services'] = registry.bind(tool.service_bindings, tool.service_capabilities)
        else:
            local_services['tool_services'] = MappingProxyType({})
        configurations.append((tool, _plain(tool.local_values), MappingProxyType(local_services)))
    return tuple(configurations)


def tool_operation_values(plan, config):
    """Apply explicitly declared local-output activations to an operation copy."""
    from api_agent.config.resolve import ResolvedValue
    values, fields, requested = dict(config.values), dict(config.fields), {}
    for tool in plan.tools:
        for binding in tool.pending.values():
            for name, value in binding.get('activation', {}).items():
                declaration = plan.schema.fields.get(name)
                if declaration is None or declaration.type not in ('bool', 'int', 'str'):
                    raise ValueError('Tool activation requires a declared scalar field')
                expected = {'bool': bool, 'int': int, 'str': str}[declaration.type]
                if type(value) is not expected or declaration.sensitive:
                    raise ValueError('Tool activation differs from its declared field type')
                if name in requested and (type(requested[name]) is not type(value) or requested[name] != value):
                    raise ValueError('Selected tool activations conflict')
                current = fields.get(name)
                if current is not None and current.explicit and (type(current.value) is not type(value) or current.value != value):
                    raise ValueError('Explicit infrastructure activation disables a selected tool')
                requested[name] = value
    for name, value in requested.items():
        values[name] = value
        fields[name] = ResolvedValue(value, 'operation:tools', False)
    effective = replace(config, values=values, fields=fields, tool_plan=None)
    return replace(effective, tool_plan=replace(plan, config=effective))


def captured_tool_manifest(path, body, owner):
    """Validate one captured manifest through the existing preparation contract."""
    import tomllib
    from api_agent.providers.distribution import ProviderDependencyManifest
    from api_agent.runtime.dependencies import _parsed_requirement
    data = tomllib.loads(body.decode('utf-8'))
    if (set(data) - {'version', 'dependencies', 'groups'} or type(data.get('version')) is not int
            or data['version'] != 1 or type(data.get('dependencies', [])) is not list
            or type(data.get('groups', {})) is not dict):
        raise ValueError('Invalid selected tool dependency manifest')
    dependencies, groups = data.get('dependencies', []), data.get('groups', {})
    if any(type(name) is not str or not name or type(items) is not list for name, items in groups.items()):
        raise ValueError('Invalid selected tool dependency group')
    for requirement in (*dependencies, *(item for items in groups.values() for item in items)):
        _parsed_requirement(requirement)
    return ProviderDependencyManifest(path.parent, owner, tuple(dependencies),
        MappingProxyType({name: tuple(items) for name, items in groups.items()}))


def selected_tool_manifests(plan):
    """Project captured manifests into the existing dependency-preparation owner."""
    manifests = {}
    for tool in plan.tools:
        for path, body in tool.admission.assets.items():
            if path.name == 'provider-dependencies.toml':
                manifests[path] = captured_tool_manifest(path, body, tool.id)
    return tuple(manifests.values())
