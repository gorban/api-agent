"""Protected collector input receipts; shared planners consume captured bytes only."""
import base64
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path

from .infrastructure_contract import (CapturedAsset, _capture, _relative,
    MAX_CAPTURE_BYTES, MAX_DECLARATION_BYTES, MAX_SERVICES, parse_infrastructure_sources)
from .ownership import StackScope

MAX_PACKET_BYTES = 512 * 1024
BASE_NAMES = frozenset({'compose.json', 'empty.env', 'localstack-ready.sh', 'jaeger-config.json'})


def _root(value):
    if (type(value) is not str or not value or len(value) > 4096
            or not Path(value).is_absolute() or str(Path(value)) != value or '..' in Path(value).parts):
        raise ValueError('Invalid captured collector owner root')
    return Path(value)


def _row(root, asset):
    return dict(root=str(root), path=asset.relative_path,
                body=base64.b64encode(asset.body).decode('ascii'), sha256=asset.sha256)


def _asset(row):
    if type(row) is not dict or set(row) != {'root', 'path', 'body', 'sha256'}:
        raise ValueError('Invalid captured collector asset')
    root = _root(row['root'])
    _relative(row['path'], 'collector asset')
    if type(row['body']) is not str or type(row['sha256']) is not str:
        raise ValueError('Invalid captured collector asset receipt')
    try:
        body = base64.b64decode(row['body'], validate=True)
    except ValueError:
        raise ValueError('Invalid captured collector asset encoding') from None
    if len(body) > MAX_CAPTURE_BYTES or sha256(body).hexdigest() != row['sha256']:
        raise ValueError('Captured collector asset digest differs')
    return (root, row['path']), CapturedAsset(row['path'], body, row['sha256'])


def _budget(packet):
    try:
        size = len(json.dumps(packet, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise ValueError('Invalid captured collector packet') from None
    if size > MAX_PACKET_BYTES:
        raise ValueError('Captured collector packet exceeds its protected snapshot budget')


def _schema(context, config):
    from api_agent.config import load_shared_schema
    from api_agent.providers.loading import bootstrap_schema
    from .pack_admission import configuration_inputs
    from .pack_composition import load_context_schema
    schema = load_context_schema(context, load_shared_schema(context.base_root))
    return bootstrap_schema(schema, (), {}, {}, config, owner_root=context.owner_root,
        configuration_paths=context.provider_config_paths, **configuration_inputs(context))


def capture_collector_plan(context, config, tool_plan=None, *, trace_context=None):
    """Capture immutable declaration inputs in the existing protected launch channel."""
    from .inventory import capture_inventory
    from .compose import default_compose_directory
    resolved = config
    config = config.values if hasattr(config, 'fields') else config
    if not config.get('TRACE_PROVIDER'):
        return None
    from api_agent.telemetry.tracing_providers import effective_selection
    from .tracing_services import selected_tracing_infrastructure
    selection = effective_selection(resolved, owner_root=context.owner_root, **(trace_context or {}))
    if selection is None:
        return None
    from .tracing_context import capture_trace_context
    capture_trace_context(selection)
    config, path, descriptor = selection.values, selection.path, selection.declaration
    secret_fields = descriptor.secret_fields
    if descriptor.dashboard is not None:
        secret_fields = (*secret_fields, descriptor.dashboard.username_field, descriptor.dashboard.password_field)
    secrets = [config[name] for name in secret_fields if type(config.get(name)) is str and config[name]]
    packet = dict(version=2, base_root=str(context.base_root), pack_root=str(context.pack_root),
        selection=sha256(config['TRACE_PROVIDER'].encode()).hexdigest(), identity=selection.identity,
        descriptor=_row(path.parent, descriptor.source), selected=[], sources=[], assets=[],
        schema={'version':1, 'fields':{}}, inventory=None, base_root_assets=None, base_assets=[])
    # A small positive remote receipt distinguishes remote selection from a lost
    # managed receipt without reading any schema, infrastructure or engine state.
    if descriptor.infrastructure or descriptor.managed_compose_profile:
        contributions, admitted = selected_tracing_infrastructure(context, config, selection=selection)
        schema = _schema(context, config) if contributions else None
        fields = {item.activation.field for item in contributions if item.activation is not None}
        packet['schema']['fields'] = {name:{'type':schema.fields[name].type} for name in sorted(fields)}
        packet['selected'] = [item.id for item in contributions]
        packet['inventory'] = capture_inventory(admitted, tool_plan).to_wire()
        assets, sources = {}, {}
        for item in contributions:
            root = next(Path(row['root']) for row in packet['inventory']['authority']
                        if Path(row['root']) / row['path'] == item.source and row['owner'] == item.owner)
            sources[(root, item.declaration_source.relative_path)] = item.owner
            for asset in (item.declaration_source, item.compose_source, *item.assets):
                key = (root, asset.relative_path)
                if key in assets and assets[key] != asset:
                    raise ValueError('Conflicting captured collector asset identity')
                assets[key] = asset
        packet['sources'] = [dict(root=str(root), path=relative, owner=owner)
            for (root, relative), owner in sorted(sources.items())]
        packet['assets'] = [_row(root, asset) for (root, _), asset in sorted(assets.items())]
        directory = default_compose_directory().resolve()
        packet['base_root_assets'] = str(directory)
        packet['base_assets'] = [_row(directory, _capture(directory, name, 1024 * 1024)) for name in sorted(BASE_NAMES)]
        if schema is not None:
            secrets.extend(value for name, value in config.items() if type(value) is str and value
                           and name in schema.fields and schema.fields[name].sensitive)
    _decode(packet, config, owner_root=context.owner_root,
            trace_context={'environ': selection.environment, 'explicit_fields':selection.explicit_fields})
    _screen_packet(packet, tuple(set(secrets)))
    return packet


def _screen_packet(packet, secrets):
    """Reject required source bytes rather than silently sanitizing their receipts."""
    from api_agent.providers.scrubbed_response import scrub_response
    from api_agent.telemetry.redaction import registered_byte_ranges
    def check(body):
        if registered_byte_ranges(body, secrets):
            raise ValueError('Collector packet may not contain configured credentials')
    def visit(value):
        if type(value) is str:
            check(value.encode('utf-8'))
            # Compare against ordinary structural admission so harmless source
            # placeholders are not mistaken for configured credential values.
            schema = {'type': 'string'}
            if scrub_response(value, schema=schema, secrets=secrets)[0] != scrub_response(value, schema=schema)[0]:
                raise ValueError('Collector packet may not contain configured credentials')
        elif type(value) is dict:
            for key, child in value.items():
                visit(key)
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)
    visit(packet)
    check(json.dumps(packet, allow_nan=False).encode())
    for row in (packet['descriptor'], *packet['assets'], *packet['base_assets']):
        check(_asset(row)[1].body)


def _decode(packet, config, *, owner_root=None, trace_context=None):
    from api_agent.config import schema_from_dict
    from api_agent.telemetry.tracing_providers import parse_declaration, effective_selection
    from .inventory import InventoryCatalog
    from .tracing_services import selected_closure
    _budget(packet)
    keys = {'version','base_root','pack_root','selection','identity','descriptor','selected','sources',
            'assets','schema','inventory','base_root_assets','base_assets'}
    if (type(packet) is not dict or set(packet) != keys or type(packet['version']) is not int or packet['version'] != 2
            or type(config.get('TRACE_PROVIDER')) is not str
            or packet['selection'] != sha256(config['TRACE_PROVIDER'].encode()).hexdigest()):
        raise ValueError('Invalid selected collector receipt')
    base, pack = _root(packet['base_root']), _root(packet['pack_root'])
    (provider_root, name), source = _asset(packet['descriptor'])
    if name != 'tracing-provider.toml' or len(source.body) > MAX_DECLARATION_BYTES:
        raise ValueError('Invalid captured tracing descriptor')
    descriptor = parse_declaration(source)
    selection = effective_selection(config, owner_root=owner_root or pack, declaration=descriptor, **(trace_context or {}))
    if selection is None or selection.identity != packet['identity']:
        raise ValueError('Captured tracing profile or destinations differ')
    descriptor = selection.declaration
    if (type(packet['schema']) is not dict or set(packet['schema']) != {'version','fields'}
            or type(packet['schema']['fields']) is not dict or len(packet['schema']['fields']) > MAX_SERVICES):
        raise ValueError('Invalid collector activation schema')
    if (type(packet['selected']) is not list or len(packet['selected']) > MAX_SERVICES
            or any(type(name) is not str for name in packet['selected'])
            or len(set(packet['selected'])) != len(packet['selected'])
            or type(packet['sources']) is not list or type(packet['assets']) is not list
            or len(packet['sources']) > 16 or len(packet['assets']) > 560
            or type(packet['base_assets']) is not list):
        raise ValueError('Invalid captured collector inputs')
    if not descriptor.infrastructure and not descriptor.managed_compose_profile:
        if (packet['selected'] or packet['sources'] or packet['assets'] or packet['base_assets']
                or packet['inventory'] is not None or packet['base_root_assets'] is not None
                or packet['schema'] != {'version':1,'fields':{}}):
            raise ValueError('Remote collector receipt contains local infrastructure')
        return None
    scope = StackScope.from_roots(base, pack)
    if type(packet['inventory']) is not dict or 'authority' not in packet['inventory']:
        raise ValueError('Managed collector inventory is absent')
    inventory = InventoryCatalog.from_wire(packet['inventory'],scope=scope,
                                          authority=packet['inventory']['authority'])
    if descriptor.managed_compose_profile:
        if (packet['selected'] or packet['sources'] or packet['assets']
                or packet['schema'] != {'version':1, 'fields':{}}):
            raise ValueError('Controlled collector receipt contains custom infrastructure')
        directory, base_assets = _base_assets(packet)
        from .compose import _validate_tracing_destination, _tracing_destination
        _validate_tracing_destination(descriptor.managed_compose_profile, _tracing_destination(selection),
                                      selection.values, json.loads(base_assets['compose.json']))
        return scope, inventory, (), schema_from_dict(packet['schema']), directory, base_assets, selection
    authority = {(Path(row['root']),row['path']):(row['owner'],body)
                 for row, body in zip(inventory.authority, inventory.declarations, strict=True)}
    assets = {}
    for row in packet['assets']:
        key, asset = _asset(row)
        if key in assets:
            raise ValueError('Duplicate captured collector asset')
        assets[key] = asset
    used, sources, owners = set(), [], {}
    for row in packet['sources']:
        if type(row) is not dict or set(row) != {'root','path','owner'}:
            raise ValueError('Invalid collector declaration authority')
        root = _root(row['root'])
        _relative(row['path'], 'collector declaration')
        key = root, row['path']
        if key not in authority or authority[key][0] != row['owner'] or key in sources:
            raise ValueError('Collector source differs from captured inventory authority')
        if root in owners and owners[root] != row['owner']:
            raise ValueError('Collector root has conflicting owners')
        owners[root] = row['owner']
        sources.append(key)
    def capture(root, relative, limit):
        key = Path(root), relative
        asset = assets.get(key)
        if asset is None or len(asset.body) > limit:
            raise ValueError('Required collector asset is absent or exceeds its budget')
        if key in authority and (asset.body.decode('utf-8') != authority[key][1]['body']
                                 or asset.sha256 != authority[key][1]['sha256']):
            raise ValueError('Collector declaration differs from inventory bytes')
        used.add(key)
        return asset
    contributions = parse_infrastructure_sources(sources,capture=capture,expected_owners=owners,
                                                  selected_ids=set(packet['selected']))
    ordered = selected_closure(contributions, descriptor.infrastructure)
    if set(packet['selected']) != {item.id for item in ordered} or used != set(assets):
        raise ValueError('Collector receipt differs from its declared selected closure')
    if any(item.owner != descriptor.id for item in ordered if item.source.parent == provider_root):
        raise ValueError('Collector provider owner differs from its descriptor')
    schema = schema_from_dict(packet['schema'])
    expected_fields = {item.activation.field for item in ordered if item.activation is not None}
    if (set(schema.fields) != expected_fields or any(set(row) != {'type'} for row in packet['schema']['fields'].values())
            or any(field.type not in {'str','bool','int'} for field in schema.fields.values())):
        raise ValueError('Collector activation schema differs from selected declarations')
    directory, base_assets = _base_assets(packet)
    return scope, inventory, ordered, schema, directory, base_assets, selection


def _base_assets(packet):
    directory = _root(packet['base_root_assets'])
    base_assets = {}
    for row in packet['base_assets']:
        (root, name), asset = _asset(row)
        if root != directory or name in base_assets:
            raise ValueError('Invalid controlled collector asset authority')
        base_assets[name] = asset.body
    if set(base_assets) != BASE_NAMES:
        raise ValueError('Required controlled collector assets are absent')
    return directory, base_assets


def verify_collector_binding(snapshot, binding):
    """Bind the already admitted factory descriptor to its captured collector receipt."""
    packet = snapshot.get('trace_collector_plan')
    if type(packet) is dict and packet.get('version') == 2:
        if binding is None:
            raise ValueError('Selected collector binding is absent')
        _decode(packet, binding.values, owner_root=binding.owner_root,
                trace_context={'environ': binding._environment, 'explicit_fields': binding.explicit_fields})
        if (packet['descriptor']['sha256'] != binding.declaration.source.sha256
                or packet['descriptor']['root'] != str(binding.owner_root)):
            raise ValueError('Tracing binding differs from captured collector descriptor')


def collector_from_snapshot(snapshot, config):
    """Restore an enabled selection's receipt; disabled callers skip this boundary.

    An explicitly selected provider requires its captured descriptor even for a
    remote result. Missing active receipts never fall back to mutable sources.
    """
    from .compose import ENGINE_ENVIRONMENT_KEYS, _tracing_destination
    from .prerequisites import PrerequisiteManager
    packet = snapshot.get('trace_collector_plan')
    if config.get('TRACE_PROVIDER'):
        from .tracing_context import restore_trace_context
        decoded = _decode(packet, config, owner_root=_root(snapshot['owner_root']),
                          trace_context=restore_trace_context(snapshot, required=True, config=config))
        if decoded is None:
            return None
        scope, inventory, contributions, schema, directory, assets, selection = decoded
        from .infrastructure_plan import plan_infrastructure
        environment = {key:os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}
        selected = {**selection.values, 'ALLURE_S3_USE_LOCALSTACK':False}
        profile = selection.declaration.managed_compose_profile
        plan = plan_infrastructure(selected,schema,contributions,scope,compose_directory=directory,
                                  engine_environment=environment,inventory=inventory,base_assets=assets,
                                  tracing_profiles=(profile,) if profile else (),
                                  tracing_destination=_tracing_destination(selection))
        if {item.id for item in plan.conditional} != {item.id for item in contributions}:
            raise ValueError('Selected collector activation is unavailable')
    else:
        return None
    owner_root = _root(snapshot['owner_root'])
    return PrerequisiteManager(plan,scope,owner_root/'artifacts'/'prerequisites',
                               engine_environment=environment,cleanup_timeout_seconds=60)
