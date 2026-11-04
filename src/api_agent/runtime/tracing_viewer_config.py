"""Typed custom dashboard selection without unrelated application requirements."""
from dataclasses import replace
from api_agent.config import ConfigSchema, resolve_config
from api_agent.config.declarations import FieldDeclaration
from api_agent.config.resolve import ConfigError, ResolvedValue
from api_agent.config.editor import serialize_value
from api_agent.telemetry.tracing_providers import read_declaration, effective_selection, select_declaration


def _controllers(predicate):
    result = set()
    for name, value in predicate.items():
        if name == 'any':
            for row in value:
                result.update(_controllers(row))
        else:
            result.add(name)
    return result


def custom_viewer_config(schema, *, argv, environ, dotenv, anchors):
    if 'TRACE_PROVIDER' not in schema.fields:
        return None
    from api_agent.config.options import parse_options
    options = parse_options(schema, argv).values
    selector = resolve_config(ConfigSchema({'TRACE_PROVIDER': schema.fields['TRACE_PROVIDER']}),
        environ=environ, dotenv=dotenv, anchors=anchors, interactive=True,
        overrides={'TRACE_PROVIDER': options['TRACE_PROVIDER']} if 'TRACE_PROVIDER' in options else None)
    selector.raise_for_errors()
    location = selector.values.get('TRACE_PROVIDER')
    if not location:
        return None
    from api_agent.providers.loading import bootstrap_schema, provider_location
    owner_root = anchors.get('owner_root') or anchors.get('pack_root') or anchors.get('base_root')
    schema = bootstrap_schema(schema, argv, environ, dotenv, owner_root=owner_root)
    path, _ = provider_location(location, owner_root=owner_root)
    declaration = read_declaration(path.parent)
    options = parse_options(schema, argv)
    if options.errors:
        raise ConfigError('\n'.join(options.errors))
    if options.positionals:
        raise ConfigError('Trace viewer requires valid selected dashboard configuration and no positional arguments.')

    def resolve_fields(names, *, fields=None, pinned=None, strict=True):
        fields = dict(schema.fields if fields is None else fields)
        pinned = pinned or {}
        if names - fields.keys():
            raise ConfigError('Tracing dashboard names undeclared configuration fields.')
        while True:
            previous = set(names)
            for name in previous:
                field = fields[name]
                for predicate in (field.applies_when, field.required_when, *(row['when'] for row in field.conditional_defaults)):
                    names.update(_controllers(predicate))
            if names == previous:
                break
            if names - fields.keys():
                raise ConfigError('Tracing dashboard has an unavailable configuration controller.')
        viewer_argv = tuple('--' + name.lower().replace('_', '-') + '=' + value
                            for name, value in options.values.items() if name in names)
        result = resolve_config(ConfigSchema({name: fields[name] for name in names}), argv=viewer_argv,
            environ=environ, dotenv=dotenv, anchors=anchors, interactive=True,
            overrides={**{name: value for name, value in {'TUI_MODE': 'run-local', 'COLLECT_ONLY': 'False'}.items()
                          if name in names},
                       **{name: serialize_value(value.value) for name, value in pinned.items() if name in names}})
        if strict:
            result.raise_for_errors()
            if result.missing:
                raise ConfigError('Trace viewer requires valid selected dashboard configuration and no positional arguments.')
        return replace(result, fields={**result.fields, **{name: value for name, value in pinned.items()
                                                         if name in result.fields}})

    names = {'TRACE_PROVIDER'}
    if declaration.selector is not None:
        names.add(declaration.selector)
        selector_config = resolve_fields(set(names))
        try:
            selected = select_declaration(declaration, selector_config.values)
        except ValueError:
            raise ConfigError('Invalid trace destination configuration.') from None
        if selected is None:
            secrets = _raw_diagnostic_secrets(declaration.profiles, schema, (options.values, environ, dotenv))
            message = (f'Trace viewer requires an enabled tracing provider profile. '
                       f'TRACE_PROVIDER={location!r}; {declaration.selector} is disabled.')
            raise ConfigError(_scrub_diagnostic(message, secrets))
    else:
        selected = declaration
    selector_names = set(selector_config.fields) if declaration.selector is not None else set(names)
    names.update(selector_names)
    names.update({'CLIENT_CA_FILE'} & schema.fields.keys())
    dashboard = selected.dashboard
    if dashboard is not None:
        names.update(value for value in (dashboard.url_field, dashboard.readiness_field, dashboard.username_field, dashboard.password_field) if value)
        names.update(dashboard.config_fields)
    strict_endpoint = selected.endpoint_field in names
    fields, pinned = dict(schema.fields), {}
    managed = dict(selected.managed_endpoints)
    port = managed.get('dashboard_port_field')
    if managed or dashboard is None:
        auxiliary = {name for name in (selected.endpoint_field if not strict_endpoint else None,
                                       port if port not in names else None) if name in schema.fields}
        auxiliary_fields = {name: replace(field, required=False, required_when={})
                            for name, field in schema.fields.items()}
        try:
            aux = resolve_fields(auxiliary, fields=auxiliary_fields, strict=False)
            valid = not aux.errors and auxiliary <= aux.values.keys()
        except ConfigError:
            valid = False
        if not strict_endpoint:
            endpoint = selected.endpoint_field
            endpoint_value = ''
            if valid:
                from api_agent.telemetry.genai_metrics import signal_endpoint
                explicit = frozenset(name for name, value in aux.fields.items() if value.explicit)
                endpoint_value = signal_endpoint(aux.values, selected, 'traces', environ=environ,
                                                 explicit_fields=explicit, required=False) or ''
            origin = aux.fields.get(endpoint) if valid else None
            pinned[endpoint] = ResolvedValue(endpoint_value, origin.source if origin else 'viewer_auxiliary',
                                            origin.explicit if origin and endpoint_value else True)
            fields[endpoint] = FieldDeclaration(endpoint, 'str', default=endpoint_value,
                sensitive=fields[endpoint].sensitive if endpoint in fields else False)
            names.add(endpoint)
        if port and port not in names:
            if valid and port in aux.fields:
                pinned[port] = aux.fields[port]
                fields[port] = replace(fields[port], required=False, required_when={}, applies_when={},
                                       conditional_defaults=(), default=aux.values[port])
            else:
                pinned[port] = ResolvedValue(0, 'viewer_auxiliary', True)
                fields[port] = FieldDeclaration(port, 'int', default=0)
            names.add(port)

    def admit(result):
        try:
            return effective_selection(result, owner_root=owner_root, environ=environ,
                                       declaration=declaration, require_export=False)
        except ValueError:
            raise ConfigError('Invalid trace destination configuration.') from None

    result = resolve_fields(set(names), fields=fields, pinned=pinned)
    try:
        selection = admit(result)
    except ConfigError:
        if not managed or strict_endpoint:
            raise
        endpoint = selected.endpoint_field
        pinned[endpoint] = ResolvedValue('', 'viewer_auxiliary', True)
        fields[endpoint] = replace(fields[endpoint], default='')
        result = resolve_fields(set(names), fields=fields, pinned=pinned)
        selection = admit(result)
    if selection.declaration.dashboard is None:
        # Export-only credentials must not become dashboard prerequisites, but
        # configured secrets still constrain what a diagnostic may display.
        secrets = _raw_diagnostic_secrets((selected,), schema, (options.values, environ, dotenv))
        for name in set(selected.secret_fields) & schema.fields.keys():
            try:
                secrets.append(resolve_fields({name}, strict=False).values.get(name))
            except ConfigError:
                pass
        raise ConfigError(_dashboard_unavailable(selection, selected, declaration, secret_values=secrets))

    def ownership(value):
        descriptor = value.declaration
        return (value.location, descriptor.source.sha256, descriptor.profile,
                value.values.get(descriptor.endpoint_field),
                value.values.get(descriptor.dashboard.url_field) if descriptor.dashboard else None,
                descriptor.managed_compose_profile, descriptor.infrastructure)

    if selection.declaration.infrastructure or selection.declaration.managed_compose_profile:
        managed_fields = selection.declaration.dashboard.managed_config_fields
        before = ownership(selection)
        if set(managed_fields) - schema.fields.keys():
            raise ConfigError('Tracing dashboard names undeclared configuration fields.')
        # Managed dependencies retain their ordinary strict declarations.
        for name in managed_fields:
            if name in pinned:
                pinned.pop(name)
                fields[name] = schema.fields[name]
        result = resolve_fields(set(names) | set(managed_fields), fields=fields, pinned=pinned)
        selection = admit(result)
        if selection is None or ownership(selection) != before:
            raise ConfigError('Tracing dashboard managed configuration changed admitted ownership.')
    dashboard = selection.declaration.dashboard
    if dashboard.username_field is not None and any(
            type(selection.values.get(name)) is not str or not selection.values[name].strip()
            or '\x00' in selection.values[name] for name in (dashboard.username_field, dashboard.password_field)):
        raise ConfigError('Trace viewer requires valid selected dashboard login credentials.')
    from api_agent.runtime.local_status import safe_destination
    if safe_destination(selection.values.get(selection.declaration.dashboard.url_field)) is None:
        raise ConfigError('Tracing dashboard requires a valid HTTP(S) URL without credentials, query or fragment.')
    return dict(selection.values)


def _dashboard_unavailable(selection, profile, declaration, *, secret_values=()):
    """Describe admitted capability loss without displaying unsafe URL components."""
    from api_agent.runtime.local_status import safe_destination
    values = selection.values
    endpoint = profile.endpoint_field
    secrets = [*secret_values, *(values.get(name) for name in profile.secret_fields)]
    if profile.dashboard and profile.dashboard.password_field:
        secrets.append(values.get(profile.dashboard.password_field))

    def safe_url(value):
        return (safe_destination(value) if type(value) is str and not any(
            type(secret) is str and secret and secret in value for secret in secrets) else None)

    actual = safe_url(values.get(endpoint)) or '<unset or unsafe URL; redacted>'
    parts = ['Selected tracing provider has no dashboard capability.',
             f'TRACE_PROVIDER={selection.location!r}.']
    if declaration.selector:
        parts.append(f'{declaration.selector}={profile.profile!r}.')
    parts.append(f'Actual {endpoint}={actual}.')
    managed = dict(profile.managed_endpoints)
    expected = [managed['trace_endpoint'], *managed.get('trace_endpoint_aliases', ())] if managed else []
    if expected:
        parts.append('Managed endpoint defaults: ' + ', '.join(
            safe_url(value) or '<unsafe URL; redacted>' for value in expected) + '.')
    if selection.dashboard_unavailable_reason == 'explicit_empty_viewer':
        parts.append(f'{profile.dashboard.url_field} is explicitly empty, which disables the dashboard; '
                     'remove that saved override to use the managed default, or set an explicit dashboard URL.')
    elif selection.dashboard_unavailable_reason == 'unmanaged_endpoint':
        parts.append('The effective ingestion endpoint does not match this profile’s managed defaults. '
                     'A saved endpoint from a different profile can remain after changing the mode. '
                     f'Review {endpoint}: remove the saved override to use the selected profile’s default '
                     f'or keep remote ingestion and configure an explicit {profile.dashboard.url_field}. '
                     'Explicit endpoints are never overwritten automatically.')
    return _scrub_diagnostic(' '.join(parts), secrets)


def _raw_diagnostic_secrets(profiles, schema, sources):
    """Read known raw secrets without admitting disabled credential requirements."""
    names = {name for profile in profiles for name in profile.secret_fields}
    names.update(name for name, field in schema.fields.items() if field.sensitive)
    names.update(profile.dashboard.password_field for profile in profiles if profile.dashboard)
    fields = [field for name, field in schema.fields.items() if name in names]
    defaults = [field.default for field in fields]
    defaults.extend(row['value'] for field in fields for row in field.conditional_defaults)
    return defaults + [source.get(name) for name in names for source in sources]


def _scrub_diagnostic(message, secrets):
    for secret in sorted({value for value in secrets if type(value) is str and value}, key=len, reverse=True):
        message = message.replace(secret, '[redacted]')
    return message
