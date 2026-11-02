"""Ordered pack contributions, with explicit responsibility replacement."""
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
import tomllib

from .resource_pack import ResourcePackError


def validate_replacements(declaration):
    if declaration is None:
        return ()
    if (type(declaration) is not dict or set(declaration) != {'version', 'replace'}
            or type(declaration['version']) is not int or declaration['version'] != 1
            or type(declaration['replace']) is not list
            or any(type(item) is not str or item not in {'ui', 'api', 'workflow'}
                   for item in declaration['replace'])
            or len(set(declaration['replace'])) != len(declaration['replace'])):
        raise ResourcePackError('composition requires version=1 and unique ui/api/workflow replacements')
    return tuple(declaration['replace'])


def load_ordered_context(base_root, directories, launcher_root=None):
    from .resource_pack import load_launch_context
    if not directories:
        raise ResourcePackError('Ordered packs must not be empty')
    layers = tuple(load_launch_context(base_root, Path(path), launcher_root) for path in directories)
    if len({layer.pack_root for layer in layers}) != len(layers):
        raise ResourcePackError('Ordered packs must have unique physical roots')
    if len({layer.pack_id for layer in layers}) != len(layers):
        raise ResourcePackError('Ordered packs must have unique ids')
    if len(layers) == 1:
        return layers[0]
    workflow = None
    tooling = None
    identity = None
    declared = set()
    defaults, tests = {}, {}
    for layer in layers:
        if layer is not layers[-1]:
            _validate_auxiliary_ownership(layer)
        fields = set(tomllib.loads(layer.config_path.read_text()).get('fields', {}))
        for path in layer.provider_config_paths:
            fields.update(tomllib.loads(path.read_text()).get('fields', {}))
        for responsibility, field in (('ui', 'UI_PROVIDER'), ('api', 'API_PROVIDER')):
            if field in fields or field in layer.defaults:
                if responsibility in declared and responsibility not in layer.composition_replacements:
                    raise ResourcePackError(f'{responsibility} replacement must be explicit')
                declared.add(responsibility)
        if layer.workflow_entry is not None:
            if workflow is not None and 'workflow' not in layer.composition_replacements:
                raise ResourcePackError('workflow replacement must be explicit')
            workflow = layer
        if layer.tool_catalog_path is not None:
            if tooling is not None:
                raise ResourcePackError('Multiple tool catalog owners are unsupported')
            tooling = layer
        if layer.identity_factory_path is not None:
            identity = layer
        defaults.update(layer.defaults)
        for mode, paths in layer.tests.items():
            tests[mode] = tuple(dict.fromkeys((*tests.get(mode, ()), *paths)))
    if tooling is not None and workflow is not None and tooling.pack_root != workflow.pack_root:
        raise ResourcePackError('Tool catalog and workflow must share one pack owner')
    selected = layers[-1]
    graph = workflow or selected
    return replace(selected, pack_layers=layers, defaults=MappingProxyType(defaults),
        tests=MappingProxyType(tests),
        provider_config_paths=_collect(layers, 'provider_config_paths'),
        production_paths=_collect(layers, 'production_paths'),
        shared_dotenv_paths=tuple(dict.fromkeys(path for layer in layers
            for path in (*layer.shared_dotenv_paths, layer.dotenv_path))),
        credential_role_fields=_collect(layers, 'credential_role_fields'),
        infrastructure_sources=tuple((layer.pack_root, layer.infrastructure_paths) for layer in layers),
        infrastructure_paths=_collect(layers, 'infrastructure_paths'),
        inventory_sources=_collect(layers, 'inventory_sources'),
        identity_factory_path=identity.identity_factory_path if identity is not None else None,
        identity_factory_name=identity.identity_factory_name if identity is not None else None,
        identity_pack_root=identity.pack_root if identity is not None else None,
        workflow_pack_root=graph.pack_root,
        workflow_root=graph.workflow_root, workflow_entry=graph.workflow_entry,
        workflow_base_root=graph.workflow_base_root,
        workflow_library_files=graph.workflow_library_files,
        workflow_replacements=graph.workflow_replacements,
        tool_catalog_path=tooling.tool_catalog_path if tooling is not None else None)


def _validate_auxiliary_ownership(layer):
    """Auxiliary declarations currently have one consumer owner at bootstrap."""
    for path in layer.provider_config_paths:
        fields = tomllib.loads(path.read_text()).get('fields', {})
        if type(fields) is not dict:
            raise ResourcePackError('Auxiliary configuration requires a fields table')
        for name, spec in fields.items():
            if type(spec) is not dict:
                raise ResourcePackError('Auxiliary configuration requires field metadata tables')
            conditional = spec.get('conditional_defaults', [])
            if (type(conditional) is not list or any(type(row) is not dict or 'value' not in row
                                                   for row in conditional)):
                raise ResourcePackError('Auxiliary conditional defaults require value tables')
            values = ([spec['default']] if 'default' in spec else [])
            values.extend(row['value'] for row in conditional)
            for value in values:
                if not isinstance(value, str) or not value:
                    continue
                if spec.get('type') == 'path' and spec.get('anchor') != 'base_root':
                    selected = value
                elif name.endswith('_PROVIDER') and ':' in value:
                    selected = value.rsplit(':', 1)[0]
                else:
                    continue
                if not Path(selected).is_absolute():
                    raise ResourcePackError(
                        'Inherited auxiliary configuration has an owner-relative default; '
                        'use an absolute default or move it into the owning pack configuration')


def _collect(layers, field):
    return tuple(dict.fromkeys(item for layer in layers for item in getattr(layer, field)))


def load_context_schema(context, shared):
    """Resolve each declaration's defaults in its owning pack before layering."""
    from api_agent.config import ConfigSchema, compose_schema, load_declarations
    from api_agent.config.declarations import UNSET
    if not context.pack_layers:
        return compose_schema(shared, load_declarations(context.config_path, base_schema=shared,
                              source='pack_default'), overrides=context.defaults)
    schema = shared
    for layer in context.pack_layers or (context,):
        extension = load_declarations(layer.config_path, base_schema=schema, source='pack_default')
        schema = compose_schema(schema, extension, overrides=layer.defaults)
        fields = dict(schema.fields)
        for name, declaration in fields.items():
            def rooted(value):
                if value is UNSET:
                    return value
                if declaration.type == 'path' and declaration.anchor and value:
                    return str((layer.anchors[declaration.anchor] / Path(str(value)).expanduser()).resolve())
                if name.endswith('_PROVIDER') and isinstance(value, str) and ':' in value:
                    path, factory = value.rsplit(':', 1)
                    return str((layer.owner_root / Path(path).expanduser()).resolve()) + ':' + factory
                return value
            fields[name] = replace(declaration, default=rooted(declaration.default),
                conditional_defaults=tuple(dict(row, value=rooted(row['value']))
                                           for row in declaration.conditional_defaults))
        schema = ConfigSchema(fields)
    return schema
