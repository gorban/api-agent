"""Discover inert graph catalogs, then compose only selected named tool inputs."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from api_agent.config.declarations import ConfigSchema, FieldDeclaration, TYPES, UNSET, schema_from_dict
from api_agent.config.options import parse_options
from api_agent.config.resolve import _matches, convert_value, resolve_config
from api_agent.runtime.resource_pack import load_selected_definition
from api_agent.testing.workflow_selection import parse_catalog, selected_graphs
from .declarations import Capture, instance_declaration, implementation_declaration
from .models import (SelectedTool, ToolCatalog, ToolPlan, ToolSelectionError,
                     operation_selection, freeze)

MAX_INDEX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_INDEX_CAPTURE_FILES = 4096


def discover(context, selection=None):
    """Capture available declarations separately from the selected graph closure."""
    operation = operation_selection(selection)
    roots = {'base': getattr(context, 'workflow_base_root', None) or context.base_root,
             'pack': context.pack_root}
    capture = Capture(roots, library_files=getattr(context, 'workflow_library_files', None))
    definitions, diagnostics, selected, profiles, selected_profiles = {}, {}, (), {}, ()
    path = getattr(context, 'tool_catalog_path', None)
    declaration = None
    case_graphs, case_profiles = {}, {}
    indexed, pool, indexed_size = {}, {}, 0
    def retain(definition):
        nonlocal indexed_size
        additions = {key: body for key, body in definition.contents.items() if key not in indexed}
        if (len(indexed) + len(additions) > MAX_INDEX_CAPTURE_FILES
                or indexed_size + sum(map(len, additions.values())) > MAX_INDEX_CAPTURE_BYTES):
            raise ToolSelectionError('Catalog resource index exceeds capture budget')
        if any(key in indexed and indexed[key] != body for key, body in definition.contents.items()):
            raise ToolSelectionError('Catalog resources changed during discovery')
        indexed.update(additions)
        indexed_size += sum(map(len, additions.values()))
        for body in additions.values():
            pool.setdefault(sha256(body).hexdigest(), body)
        contents = {key: pool[sha256(body).hexdigest()] for key, body in definition.contents.items()}
        resources = tuple(replace(item, content_bytes=pool[item.sha256]) for item in definition.resources)
        return replace(definition, contents=freeze(contents), resources=resources)
    if operation.mode == 'application':
        entry = getattr(context, 'workflow_entry', None) or roots['base'] / 'flow.md.html'
        if entry.is_file():
            try:
                definitions['@application'] = retain(load_selected_definition(context))
            except ValueError:
                diagnostics['@application'] = 'Cannot admit selected application graph'
    if path is not None:
        declaration = parse_catalog(capture.toml(Path(path)), roots)
        profiles = dict(declaration.profiles)
        selected, selected_profiles = selected_graphs(declaration, operation)
        case_graphs = {identity: (() if operation.mode == 'unit' else tuple(declaration.test_cases.get(identity, {'graphs': ()})['graphs']))
                       for identity in operation.test_ids}
        specifications = dict(declaration.graphs)
        for name, variant in declaration.variants.items():
            specifications[name] = dict(declaration.graphs[variant['graph']], replacements=variant['replacements'])
            if 'profile' in variant:
                specifications[name]['profile'] = variant['profile']
        ordered_names = tuple(dict.fromkeys((*selected, *specifications)))
        for name in ordered_names:
            spec = specifications[name]
            try:
                resource_root = roots[spec['root']] / spec['resource_root'] if 'resource_root' in spec else None
                library_files = getattr(context, 'workflow_library_files', None)
                if resource_root is not None and library_files is not None:
                    if resource_root.is_relative_to(roots['base']):
                        prefix = resource_root.relative_to(roots['base']).as_posix() + '/'
                        library_files = frozenset(name.removeprefix(prefix) for name in library_files if name.startswith(prefix))
                    else:
                        library_files = None
                graph_context = replace(context, workflow_root=spec['root'],
                    workflow_entry=(resource_root or roots[spec['root']]) / spec['entry'],
                    workflow_base_root=resource_root or getattr(context, 'workflow_base_root', None),
                    pack_root=resource_root or context.pack_root,
                    workflow_library_files=library_files,
                    workflow_replacements=tuple((row['target_id'], Path(row['entry']), row['root_id'])
                        for row in spec.get('replacements', ())))
                definitions[name] = retain(load_selected_definition(graph_context))
            except ValueError:
                diagnostics[name] = 'Cannot admit catalog graph: ' + name
        case_profiles = {identity: tuple(dict.fromkeys(
            ([declaration.test_cases[identity]['profile']] if operation.mode != 'unit' and identity in declaration.test_cases and 'profile' in declaration.test_cases[identity] else [])
            + [specifications[name]['profile'] for name in names if 'profile' in specifications[name]]))
            for identity, names in case_graphs.items()}
        extra_profiles = [specifications[name]['profile'] for name in selected if 'profile' in specifications[name]]
        selected_profiles = tuple(dict.fromkeys((*selected_profiles, *extra_profiles)))
    if declaration is None:
        if operation.mode != 'unit' and any(identity not in operation.graphless for identity in operation.test_ids):
            raise ToolSelectionError('Selected exact test identity has no static graph association')
        case_graphs = {identity: () for identity in operation.test_ids}
    if operation.mode == 'application':
        selected, selected_profiles = (), ()
        entry = getattr(context, 'workflow_entry', None) or roots['base'] / 'flow.md.html'
        if entry.is_file():
            selected = ('@application',)
    for name in selected:
        if name in diagnostics or name not in definitions:
            raise ToolSelectionError(diagnostics.get(name, 'Selected graph is unavailable'))
    # Only selected graph bodies enter the execution capture; indexing unrelated
    # roots never admits them to the operation or makes their inputs required.
    for name in selected:
        for file, body in definitions[name].contents.items():
            if isinstance(file, Path):
                capture.add(file, body)
    return ToolCatalog(roots, definitions, selected, operation, diagnostics,
                       capture.bodies, profiles, selected_profiles, case_graphs, case_profiles)


def _predicate_fields(predicate):
    fields, pending, count = set(), [(predicate, 0)], 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if count > 4096 or depth > 64:
            raise ToolSelectionError('Tool selector predicate exceeds structural budget')
        for name, expected in current.items():
            if name == 'any':
                pending.extend((branch, depth + 1) for branch in expected)
            elif name != 'operation':
                fields.add(name)
    return fields


def _controller_closure(schema, selected):
    found, visiting = set(), set()
    def visit(name):
        if name in visiting:
            raise ToolSelectionError('Tool selector controller cycle')
        if name in found:
            return
        if name not in schema.fields:
            raise ToolSelectionError('Tool selector controller is unavailable')
        if len(found) + len(visiting) >= 512:
            raise ToolSelectionError('Tool selector controller closure exceeds budget')
        visiting.add(name)
        declaration = schema.fields[name]
        if declaration.sensitive:
            raise ToolSelectionError('Sensitive configuration cannot control tool implementation')
        dependencies = _predicate_fields(declaration.applies_when) | _predicate_fields(declaration.required_when)
        for conditional in declaration.conditional_defaults:
            dependencies.update(_predicate_fields(conditional['when']))
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(name)
        found.add(name)
    for name in selected:
        visit(name)
    return found


def _selector_values(context, schema, selectors, argv, environ, dotenv, overrides, operation_intents=()):
    closure = _controller_closure(schema, selectors)
    # Parse the original tokens against every currently declared field. Only
    # discovery-controller failures block this pass; final parsing retains all
    # unknown, duplicate, missing-value and positional diagnostics.
    parsed = parse_options(schema, argv)
    controller_errors = [error for error in parsed.errors if any(
        error.startswith(name + ':') or error == 'Duplicate option --' + name.lower().replace('_', '-') for name in closure)]
    if controller_errors:
        raise ToolSelectionError('\n'.join(controller_errors))
    controller_schema = ConfigSchema({name: schema.fields[name] for name in closure})
    cli = {name: value for name, value in parsed.values.items() if name in closure}
    chosen = {name: value for name, value in (overrides or {}).items() if name in closure}
    # CLI values must retain external conversion (bool/int/JSON); encode known
    # options with equals so no deferred token can consume another option.
    tokens = tuple('--' + name.lower().replace('_', '-') + '=' + value for name, value in cli.items())
    result = resolve_config(controller_schema, argv=tokens, environ=environ, dotenv=dotenv,
                            anchors=context.anchors, overrides=chosen, interactive=True,
                            pack_launcher=context.external_pack, operation_intents=operation_intents)
    if result.errors or result.missing:
        raise ToolSelectionError('Cannot resolve selected tool implementation controllers')
    if any(name not in result.values for name in selectors):
        raise ToolSelectionError('Selected tool implementation controller is inactive')
    return result.values


def _project_predicate(predicate, aliases):
    return {aliases.get(key, key) if key != 'any' else key:
            [_project_predicate(branch, aliases) for branch in value] if key == 'any' else value
            for key, value in predicate.items()}


def _project_input(row, aliases):
    result = dict(row)
    for key in ('applies_when', 'required_when'):
        if key in result:
            result[key] = _project_predicate(result[key], aliases)
    if 'conditional_defaults' in result:
        result['conditional_defaults'] = [dict(item, when=_project_predicate(item['when'], aliases))
                                           for item in result['conditional_defaults']]
    return result


def _use_bindings(definition, instance_ids):
    direct = {}
    for use in definition.tool_usages:
        direct.setdefault((use.scope_kind, use.scope), {})[use.port] = instance_ids[use.instance]
    graph = direct.get(('graph', definition.graph_id), {})
    scoped = {('graph', definition.graph_id): dict(graph)}
    steps = {step.id: step for step in definition.layout.steps}
    def bindings(step):
        key = (step.kind, step.id)
        if key not in scoped:
            inherited = bindings(steps[step.parent]) if step.parent is not None else graph
            declared = direct.get(key, {})
            if any(port in inherited and inherited[port] != identity for port, identity in declared.items()):
                raise ToolSelectionError('Inherited tool port conflicts with a local instance binding')
            scoped[key] = dict(inherited, **declared)
        return scoped[key]
    for step in definition.layout.steps:
        bindings(step)
    return {(definition.definition_id, kind, scope, port): identity
            for (kind, scope), ports in scoped.items() for port, identity in ports.items()}



def _option_inventory(catalog, base_schema, argv, environ, dotenv, overrides):
    """Read inert input arities for dormant aliases without activating them."""
    import tomllib
    fields, instances = dict(base_schema.fields), {}
    for definition in catalog.definitions.values():
        for use in definition.tool_usages:
            try:
                data = instance_declaration(tomllib.loads(definition.contents[use.instance].decode('utf-8')))
            except (ValueError, UnicodeError):
                continue
            instances.setdefault(use.instance, data)
            for alias in data.get('bindings', {}).values():
                fields.setdefault(alias, FieldDeclaration(alias, 'str'))
            selector = data.get('selector')
            if selector:
                fields.setdefault(selector, FieldDeclaration(selector, 'str', default=data['implementation']))
    parsed = parse_options(ConfigSchema(fields), argv)
    inventory = Capture(catalog.roots)
    for path, data in instances.items():
        target = data['implementation']
        selector = data.get('selector')
        if selector:
            for source in (dotenv or {}, environ or {}, parsed.values, overrides or {}):
                if selector in source:
                    target = source[selector]
        try:
            spec = implementation_declaration(inventory.toml(inventory.path(path, target)))
        except ValueError:
            continue
        for slot, row in spec.get('inputs', {}).items():
            alias = data.get('bindings', {}).get(slot)
            if alias is not None and alias not in base_schema.fields and type(row) is dict and row.get('type') in TYPES:
                enum = row.get('enum', [])
                if isinstance(enum, list):
                    fields[alias] = FieldDeclaration(alias, row['type'], enum=tuple(enum))
    return ConfigSchema(fields)

def compose_tools(context, base_schema, *, selection=None, argv=(), environ=None, dotenv=None, overrides=None, operation_intents=()):
    """Compose declarative tool inputs before final explicit-input validation."""
    catalog = discover(context, selection)
    capture = Capture(catalog.roots, library_files=getattr(context, 'workflow_library_files', None))
    for path, body in catalog.assets.items():
        capture.add(path, body)
    definitions = tuple(catalog.definitions[name] for name in catalog.selected_graphs)
    option_inventory = _option_inventory(catalog, base_schema, argv, environ, dotenv, overrides)
    base_fields = dict(base_schema.fields)
    instances, paths, selectors, selector_names, known_fields = {}, {}, {}, {}, set(option_inventory.fields)
    # Known aliases are inventory only. Malformed unselected descriptors cannot
    # become blockers or contribute implementation/factory admission.
    import tomllib
    for definition in catalog.definitions.values():
        for use in definition.tool_usages:
            try:
                data = instance_declaration(tomllib.loads(definition.contents[use.instance].decode('utf-8')))
            except (ValueError, UnicodeError):
                continue
            known_fields.update(data.get('bindings', {}).values())
            if 'selector' in data:
                known_fields.add(data['selector'])
    for definition in definitions:
        for use in definition.tool_usages:
            data = instance_declaration(capture.toml(use.instance))
            identity = data['id']
            if identity in instances and instances[identity][0] != use.instance:
                raise ToolSelectionError('Conflicting named tool identity')
            paths[use.instance] = identity
            if identity in instances:
                continue
            instances[identity] = (use.instance, data)
            selector = data.get('selector')
            if selector is not None:
                if selector in selector_names:
                    raise ToolSelectionError('Tool implementation selectors must have distinct ownership')
                selector_names[selector] = identity
                if selector in base_fields:
                    declared = base_fields[selector]
                    if declared.type != 'str' or declared.sensitive:
                        raise ToolSelectionError('Tool implementation selector requires a nonsecret string declaration')
                    if declared.default is UNSET:
                        base_fields[selector] = replace(declared, default=data['implementation'])
                else:
                    selectors[selector] = {'type': 'str', 'default': data['implementation'],
                                          'acknowledge_default': data.get('expose_selector', False)}
    base_schema = ConfigSchema(base_fields)
    selector_schema = schema_from_dict({'version': 1, 'fields': selectors}, base_schema=base_schema)
    preliminary = ConfigSchema(dict(base_fields, **selector_schema.fields))
    values = _selector_values(context, preliminary, selector_names, argv, environ, dotenv, overrides, operation_intents) if selector_names else {}
    rows, tools, admissions = dict(selectors), [], {}
    for identity, (path, data) in instances.items():
        implementation = capture.path(path, values[data['selector']] if 'selector' in data else data['implementation'])
        spec = implementation_declaration(capture.toml(implementation))
        if spec['interface'] != data['interface']:
            raise ToolSelectionError('Selected tool interface is incompatible')
        aliases = data.get('bindings', {})
        for slot, row in spec.get('inputs', {}).items():
            if slot not in aliases:
                raise ToolSelectionError('Missing selected tool local input binding')
            alias = aliases[slot]
            if alias in rows or alias in base_schema.fields:
                raise ToolSelectionError('Conflicting configuration binding ownership')
            projected = _project_input(row, aliases)
            if slot in data.get('defaults', {}):
                projected['default'] = data['defaults'][slot]
            rows[alias] = projected
        target, _, factory_name = spec['factory'].rpartition(':')
        factory = capture.path(implementation, target)
        body = capture.read(factory)
        from .admission import capture_tool_factory
        admission_key = (factory, factory_name, capture.owner(factory))
        if admission_key not in admissions:
            admissions[admission_key] = capture_tool_factory(*admission_key)
        admission = admissions[admission_key]
        for relative, source in admission.sources.items():
            capture.add(factory.parent / relative, source)
        for asset, source in admission.assets.items():
            capture.add(asset, source)
        services, capabilities = data.get('services', {}), spec.get('services', {})
        if any(slot not in services for slot in capabilities):
            raise ToolSelectionError('Missing selected tool service capability binding')
        pending, local = {}, data.get('local', {})
        if local and local['slot'] in spec.get('inputs', {}):
            for controller, value in local.get('activation', {}).items():
                field = base_schema.fields.get(controller)
                if field is None or field.sensitive or field.type not in {'bool', 'int', 'str'}:
                    raise ToolSelectionError('Local activation requires an available nonsecret scalar controller')
                try:
                    convert_value(field, value, external=False, anchors=context.anchors)
                except ValueError:
                    raise ToolSelectionError('Local activation has an incompatible controller value') from None
            declaration_path = capture.path(path, local['declaration'])
            capture.read(declaration_path)
            pending[local['slot']] = dict(local, declaration=declaration_path)
        infrastructure = data.get('infrastructure', {})
        if infrastructure:
            owner_root = capture.path(path, infrastructure['root'])
            declarations = tuple(capture.path(owner_root / '_owner', item) for item in infrastructure['declarations'])
            for file in declarations:
                capture.read(file)
            infrastructure = dict(owner=infrastructure['owner'], owner_root=owner_root,
                                  declarations=declarations)
        tools.append(SelectedTool(identity, data['interface'], factory, factory_name, body,
            {slot: aliases[slot] for slot in spec.get('inputs', {})}, pending=pending,
            service_bindings={slot: services[slot] for slot in capabilities},
            service_capabilities=capabilities, prerequisite_roots=tuple(data.get('prerequisites', ())),
            infrastructure=infrastructure, descriptor_path=path,
            implementation_path=implementation, admission=admission))
    try:
        extension = schema_from_dict({'version': 1, 'fields': rows}, base_schema=base_schema)
    except ValueError:
        raise ToolSelectionError('Invalid selected tool input declaration') from None
    fields = dict(base_schema.fields, **extension.fields)
    shared = {}
    for name in catalog.selected_profiles:
        profile = catalog.profiles[name]
        if profile['primary'] not in instances or any(item not in instances for item in profile['secondary']):
            raise ToolSelectionError('Shared-primary profile references an unselected tool')
        for secondary in profile['secondary']:
            if secondary in shared and shared[secondary] != profile['primary']:
                raise ToolSelectionError('Conflicting shared-primary profile')
            shared[secondary] = profile['primary']
    case_shared = {}
    for identity, names in catalog.case_graphs.items():
        definition_ids = {catalog.definitions[name].definition_id for name in names}
        scoped_tools = {paths[use.instance] for definition in definitions if definition.definition_id in definition_ids
                        for use in definition.tool_usages}
        pairs = {}
        for profile_name in catalog.case_profiles.get(identity, ()):
            profile = catalog.profiles[profile_name]
            for secondary in profile['secondary']:
                pairs[secondary] = profile['primary']
        if any(secondary not in scoped_tools or primary not in scoped_tools for secondary, primary in pairs.items()):
            raise ToolSelectionError('Case profile references tools outside its admitted graph closure')
        for secondary, primary in shared.items():
            if secondary in scoped_tools and (pairs.get(secondary) != primary or primary not in scoped_tools):
                raise ToolSelectionError('Selected cases disagree about shared-primary tool ownership')
        case_shared[identity] = pairs
    if set(shared) & set(shared.values()):
        raise ToolSelectionError('Shared-primary profiles cannot form derived chains')
    # Derived aliases retain explicit source inputs but do not independently
    # require a secondary endpoint before the effective primary is acquired.
    tool_by_id = {tool.id: tool for tool in tools}
    for secondary in shared:
        for slot in ('ENDPOINT', 'HEADERS'):
            alias = tool_by_id[secondary].bindings.get(slot)
            if alias is not None:
                fields[alias] = replace(fields[alias], required=False, required_when={})
    schema = ConfigSchema(fields)
    resolved = resolve_config(schema, argv=argv, environ=environ, dotenv=dotenv,
        overrides=overrides, anchors=context.anchors, interactive=True, pack_launcher=context.external_pack,
        operation_intents=operation_intents)
    active = tuple(resolved.active_fields)
    bindings = {}
    for definition in definitions:
        bindings.update(_use_bindings(definition, paths))
    from .services import capture_prerequisites
    contributions, assets = capture_prerequisites(tuple(tools), catalog.roots)
    for path, body in assets.items():
        capture.add(path, body)
    capture.verify()
    fingerprint = sha256(json.dumps((sorted(definition.definition_id for definition in definitions),
        capture.identity()), separators=(',', ':')).encode()).hexdigest()
    return ToolPlan(tuple(tools), schema, bindings, definitions, fingerprint, str(uuid4()),
        capture.bodies, infrastructure_contributions=contributions, active_fields=active, tool_fields=tuple(dict.fromkeys((*rows, *selector_names))), shared_primary=shared,
        catalog=catalog, known_fields=tuple(sorted(known_fields)),
        inactive_fields=tuple(sorted(known_fields - set(fields))), selection=catalog.selection,
        option_schema=ConfigSchema(dict(option_inventory.fields, **fields)),
        case_definitions={identity: tuple(dict.fromkeys(catalog.definitions[name].definition_id for name in names))
                          for identity, names in catalog.case_graphs.items()},
        case_shared_primary=case_shared)
