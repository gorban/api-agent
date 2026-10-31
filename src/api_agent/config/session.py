"""Editable launch configuration retaining original external provenance."""
from dataclasses import dataclass, replace
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from .declarations import ConfigSchema, FieldDeclaration
from .dotenv import read_dotenv
from .editor import DotenvEditor, serialize_value
from .resolve import (ConfigError, ResolvedValue, convert_value, resolve_config,
                      runtime_directory_overrides, retain_runtime_directory)
from api_agent.auth.profiles import DEFAULT_PASSWORD_MANAGER, _canonical_shape, normalize_auth, validate_role_entries
from api_agent.auth.managers import registry_for_context


def _review_predicate(predicate):
    """Span execution contexts without erasing provider/feature controllers."""
    return {name: ([_review_predicate(branch) for branch in expected]
                   if name == 'any' else expected)
            for name, expected in predicate.items()
            if name not in ('TUI_MODE', 'COLLECT_ONLY', 'operation')}


@dataclass(frozen=True)
class DefaultCandidate:
    name: str
    resolved: ResolvedValue
    field: FieldDeclaration

    @property
    def value(self):
        return self.resolved.value


@dataclass(frozen=True)
class CleanupRow:
    name: str
    resolved: ResolvedValue | None
    field: FieldDeclaration
    base_present: bool
    pack_present: bool
    present: bool

    @property
    def value(self):
        return self.resolved.value if self.resolved else None

    @property
    def source(self):
        return self.resolved.source if self.resolved else None


@dataclass(frozen=True)
class ReviewSnapshot:
    """Read-only presentation data; never an operation's execution authority."""
    schema: ConfigSchema
    plan: object
    execution: object
    review: object
    saved_names: frozenset
    dotenv_inputs: tuple
    source_reads: object
    runtime_required_fields: tuple = ()
    runtime_requirement_token: object = None

    def sources_current(self):
        """Content check at delivery; timestamps do not establish freshness."""
        if not all(read_dotenv(path) == dict(entries) for path, entries in self.dotenv_inputs):
            return False
        return self.source_reads.current()


class ConfigSession:
    @property
    def password_manager_registry(self):
        """Resolve declarations for the currently selected pack on each access."""
        return registry_for_context(self.context)

    @property
    def schema(self):
        return self._schema

    @schema.setter
    def schema(self, value):
        self._schema = value
        self._provider_base_schema = value

    def __init__(self, context, schema, argv=(), environ=None):
        self.context = context
        from api_agent.tooling.discovery_cache import DefinitionCache
        self._definition_cache = DefinitionCache()
        self._tool_selection = None
        self._automatic_tool_selection = True
        self._tool_intents = ()
        self.tool_plan = None
        self.schema = schema
        self._provider_base_schema = schema
        self.argv = tuple(argv)
        self.environ = dict(os.environ if environ is None else environ)
        self._choices = {}
        self._runtime_requirements = None
        self._runtime_requirement_token = None
        launcher = getattr(context, 'launcher_root', None)
        self._dotenv_path = Path(launcher) / '.env' if launcher else Path(context.dotenv_path)
        provider_dotenv = {}
        for path in self.target_paths:
            provider_dotenv.update(read_dotenv(path))
        self._schema = self._bootstrap_schema(schema, provider_dotenv)

    def _bootstrap_schema(self, schema, dotenv, choices=None, operation_intents=()):
        from api_agent.providers.loading import bootstrap_schema
        provider_schema = bootstrap_schema(schema, self.argv, self.environ, dotenv, choices,
            owner_root=getattr(self.context, 'owner_root', self.context.pack_root),
            configuration_paths=getattr(self.context, 'provider_config_paths', ()))
        if self._automatic_tool_selection:
            from api_agent.tooling.models import OperationSelection
            effective = resolve_config(provider_schema, argv=self.argv, environ=self.environ,
                dotenv=dotenv, overrides=choices, anchors=self.context.anchors,
                interactive=True, pack_launcher=self.context.external_pack,
                operation_intents=operation_intents)
            self._tool_selection = (OperationSelection('collection')
                if effective.values.get('TUI_MODE') in {'unit', 'integration', 'e2e'} else None)
        from .tool_resolution import compose_operation_tools
        self.tool_plan = compose_operation_tools(self.context, provider_schema,
            selection=self._tool_selection, argv=self.argv, environ=self.environ,
            dotenv=dotenv, overrides=choices, operation_intents=operation_intents,
            definition_loader=self._definition_cache)
        return self.tool_plan.schema if self.tool_plan is not None else provider_schema

    def select_operation(self, tests=None):
        """Select the application or an exact test union before resolving inputs."""
        from api_agent.tooling.models import OperationSelection
        self._tool_selection = tests if isinstance(tests, OperationSelection) else None if tests is None else tuple(tests)
        self._automatic_tool_selection = False
        items = () if tests is None or isinstance(tests, OperationSelection) else self._tool_selection
        self._tool_intents = ('ui_session',) if any('requires_ui_session' in getattr(item, 'tags', ()) for item in items) else ()
        self.tool_plan = None
        return self.result()

    def review_field_names(self):
        """Keep tool applicability while ordinary review spans other modes."""
        if self.tool_plan is None:
            return frozenset(self.schema.fields)
        return frozenset(self.schema.fields) - (
            set(self.tool_plan.tool_fields) - set(self.tool_plan.active_fields))

    @property
    def dotenv_path(self):
        return self._dotenv_path

    @property
    def target_paths(self):
        base = self.context.base_root / '.env'
        pack = self.context.pack_root / '.env'
        launcher = getattr(self.context, 'launcher_root', None) or self.context.base_root
        paths = [base, *getattr(self.context, 'shared_dotenv_paths', ()),
                 Path(launcher) / '.env']
        if self.context.external_pack:
            paths.append(pack)
        return tuple(dict.fromkeys(paths))

    def switch_target(self, path=None):
        if path is None:
            paths = self.target_paths
            path = paths[(paths.index(self.dotenv_path) + 1) % len(paths)]
        path = Path(path)
        if path not in self.target_paths:
            raise ConfigError(f'Unknown dotenv target {path}')
        self._dotenv_path = path

    def _source(self, path):
        launcher = getattr(self.context, 'launcher_root', None) or self.context.base_root
        return '../' + os.path.relpath(path, Path(launcher).parent)

    @staticmethod
    def _restore(path, content):
        if content is None:
            path.unlink(missing_ok=True)
            return
        descriptor, temporary = tempfile.mkstemp(prefix='.env.rollback.', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _persist(self, values):
        target = self.dotenv_path
        previous = target.read_bytes() if target.exists() else None
        others = {path: path.read_bytes() for path in self.target_paths if path != target
                  and set(values) & DotenvEditor(path).keys()}
        changed = []
        DotenvEditor(target).update(values)
        try:
            for path in others:
                DotenvEditor(path).delete(values)
                changed.append(path)
        except Exception:
            # Restore removed sources before the destination. If restoring a
            # source fails, retain the new destination to preserve saved data.
            for path in reversed(changed):
                self._restore(path, others[path])
            self._restore(target, previous)
            raise

    @staticmethod
    def _auth_object(path, *, chosen_manager=None, registry=None):
        raw = read_dotenv(path).get('UNATTENDED_AUTH')
        if raw is None:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise ConfigError('UNATTENDED_AUTH in .env must be a JSON object of role arrays.') from None
        try:
            if chosen_manager is not None and isinstance(value, dict) and not _canonical_shape(value):
                # An explicit choice replaces superseded legacy manager fields,
                # including declarations from a pack that is no longer active.
                roles = {}
                for role, entries in value.items():
                    if not isinstance(entries, list):
                        raise ValueError('UNATTENDED_AUTH legacy roles must contain arrays.')
                    converted = []
                    for entry in entries:
                        if (not isinstance(entry, dict) or
                                set(entry) != {'manager', 'cli_path', 'vault', 'user'} or
                                not isinstance(entry['manager'], str) or
                                not isinstance(entry['cli_path'], str)):
                            raise ValueError('UNATTENDED_AUTH legacy entries require manager, cli_path, vault and user descriptors.')
                        converted.append({'vault': entry['vault'], 'user': entry['user']})
                    validate_role_entries(role, converted)
                    roles[role] = converted
                return {'password_manager': chosen_manager, 'roles': roles}
            replacement = value
            if chosen_manager is not None and isinstance(value, dict) and _canonical_shape(value):
                old_manager = value.get('password_manager')
                if (old_manager is not None and
                        (not isinstance(old_manager, dict) or set(old_manager) != {'manager', 'cli_path'} or
                         any(not isinstance(old_manager[key], str) for key in ('manager', 'cli_path')))):
                    raise ValueError('UNATTENDED_AUTH password_manager requires manager and cli_path strings.')
                replacement = {**value, 'password_manager': chosen_manager}
            canonical = normalize_auth(replacement, registry=registry)
            if isinstance(value, dict) and _canonical_shape(value):
                result = {key: canonical[key] for key in ('password_manager', 'roles') if key in value}
                if chosen_manager is not None and 'password_manager' in value:
                    result['password_manager'] = value['password_manager']
                return result
            return canonical
        except ValueError as exc:
            raise ConfigError(f'UNATTENDED_AUTH in .env: {exc}') from None

    def save_auth_role(self, role, entries):
        """Save one role, enforcing pack targets and removing older copies."""
        try:
            if not isinstance(role, str) or not role.strip():
                raise ValueError('Credential role label must be nonempty.')
            validate_role_entries(role, entries)
        except ValueError as exc:
            raise ConfigError(f'UNATTENDED_AUTH role is invalid: {exc}') from None
        registry = self.password_manager_registry
        objects = {path: self._auth_object(path, registry=registry) for path in self.target_paths}
        manager_choice = None
        for path in self.target_paths:
            raw = read_dotenv(path).get('UNATTENDED_AUTH')
            if raw is None:
                continue
            value = json.loads(raw)
            explicit = ('password_manager' in value if _canonical_shape(value)
                        else any(value.values()))
            if explicit:
                choice = normalize_auth(value, registry=registry)['password_manager']
                if manager_choice is not None and choice != manager_choice:
                    raise ConfigError('Saved .env profiles use different password managers or CLI paths; choose one shared password manager before editing accounts.')
                manager_choice = choice
        target = (self.context.pack_root / '.env'
                  if self.context.external_pack and role != 'LLM User'
                  else self.dotenv_path)
        updated = deepcopy(objects)
        updated[target].setdefault('roles', {})[role] = entries
        for path in updated:
            if path != target:
                updated[path].setdefault('roles', {}).pop(role, None)
                if not updated[path]['roles']:
                    updated[path].pop('roles')
        changed = [path for path in (target, *(path for path in self.target_paths if path != target))
                   if updated[path] != objects[path]
                   or path == target and 'UNATTENDED_AUTH' not in read_dotenv(path)]
        for path in changed:
            if path.is_symlink():
                raise ValueError(f'Refusing to edit symlink: {path}')
        snapshots = {path: path.read_bytes() if path.exists() else None for path in changed}
        try:
            for path in changed:
                if updated[path]:
                    DotenvEditor(path).update({'UNATTENDED_AUTH': json.dumps(updated[path], ensure_ascii=False, separators=(',', ':'))})
                else:
                    DotenvEditor(path).delete(['UNATTENDED_AUTH'])
        except Exception:
            # Include the failing path: an editor may mutate it before raising.
            for path in reversed(changed):
                self._restore(path, snapshots[path])
            raise
        self._choices.pop('UNATTENDED_AUTH', None)
        return self.result(_review=True)

    def save_auth_manager(self, manager_dict):
        """Persist the one shared CLI choice at the selected target."""
        try:
            registry = self.password_manager_registry
            manager = normalize_auth({'password_manager': manager_dict}, registry=registry)['password_manager']
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        objects = {path: self._auth_object(path, chosen_manager=manager, registry=registry) for path in self.target_paths}
        target = self.dotenv_path
        updated = deepcopy(objects)
        updated[target]['password_manager'] = manager
        for path in updated:
            if path != target:
                updated[path].pop('password_manager', None)
        legacy_paths = []
        for path in self.target_paths:
            raw = read_dotenv(path).get('UNATTENDED_AUTH')
            if raw is not None:
                value = json.loads(raw)
                if isinstance(value, dict) and value and not _canonical_shape(value):
                    legacy_paths.append(path)
        self._write_auth_objects(objects, updated, target, force_paths=legacy_paths)
        self._choices.pop('UNATTENDED_AUTH', None)
        return self.result(_review=True)

    def reset_auth_manager(self):
        """Delete saved manager choices; retain environment and CLI precedence."""
        registry = self.password_manager_registry
        objects = {path: self._auth_object(path, chosen_manager=DEFAULT_PASSWORD_MANAGER, registry=registry)
                   for path in self.target_paths}
        updated = deepcopy(objects)
        for value in updated.values():
            value.pop('password_manager', None)
        self._write_auth_objects(objects, updated, self.dotenv_path)
        self._choices.pop('UNATTENDED_AUTH', None)
        return self.result(_review=True)

    def _write_auth_objects(self, objects, updated, target, *, force_paths=()):
        changed = [path for path in (target, *(path for path in self.target_paths if path != target))
                   if updated[path] != objects[path] or path in force_paths]
        for path in changed:
            if path.is_symlink():
                raise ValueError(f'Refusing to edit symlink: {path}')
        snapshots = {path: path.read_bytes() if path.exists() else None for path in changed}
        try:
            for path in changed:
                if updated[path]:
                    DotenvEditor(path).update({'UNATTENDED_AUTH': json.dumps(updated[path], ensure_ascii=False, separators=(',', ':'))})
                else:
                    DotenvEditor(path).delete(['UNATTENDED_AUTH'])
        except Exception:
            for path in reversed(changed):
                self._restore(path, snapshots[path])
            raise

    def result(self, *, _cleanup=False, _review=False, operation_intents=()):
        result = self._resolve(_cleanup=_cleanup, _review=_review, operation_intents=operation_intents)[0]
        if self._runtime_requirements is None:
            return result
        current = self._resolve()[0] if _review or _cleanup or operation_intents else result
        return self._with_runtime_requirements(result, current)

    def _with_runtime_requirements(self, result, current):
        if self._runtime_requirements is None:
            return result
        names, snapshot = self._runtime_requirements
        scope = {name: value for name, value in current.values.items() if name not in names}
        if scope != snapshot or not set(names) <= self.schema.fields.keys():
            self._runtime_requirements = None
            self._runtime_requirement_token = None
            return result
        missing = tuple(name for name in names if name not in result.values
                        or result.values[name] is None
                        or isinstance(result.values[name], str) and not result.values[name].strip())
        return replace(result, missing=tuple(dict.fromkeys((*result.missing, *missing))))

    def require_fields(self, fields, *, snapshot):
        """Admit only requirements from the still-current operation snapshot."""
        from api_agent.providers.contracts import ConfigurationRequiredError
        names = ConfigurationRequiredError(fields).fields
        current = self._resolve()[0]
        if not set(names) <= self.schema.fields.keys():
            raise ConfigError('Configuration requirement references an undeclared field')
        scope = {name: value for name, value in snapshot.items() if name not in names}
        effective = {name: value for name, value in current.values.items() if name not in names}
        if scope != effective:
            return False
        self._runtime_requirements = (names, deepcopy(scope))
        self._runtime_requirement_token = object()
        return True

    @property
    def runtime_required_fields(self):
        self.result()
        return self._runtime_requirements[0] if self._runtime_requirements is not None else ()

    @property
    def runtime_requirement_token(self):
        self.result()
        return self._runtime_requirement_token if self._runtime_requirements is not None else None

    def clear_runtime_requirements(self, *, expected_token):
        """Explicitly recheck only the still-current runtime facts; preserve settings."""
        self.result()
        if expected_token is None or self.runtime_requirement_token is not expected_token:
            return False
        self._runtime_requirements = None
        self._runtime_requirement_token = None
        return True

    def auth_sources(self):
        """Effective per-component provenance, independent of the write target."""
        return self._resolve(_review=True)[1]

    def _resolve(self, *, _cleanup=False, _review=False, operation_intents=()):
        prepared = self._prepare_resolution(operation_intents)
        return self._resolve_prepared(prepared, _cleanup=_cleanup, _review=_review)

    def review_snapshot(self):
        """Project both display views from one read of the configuration sources."""
        from .source_reads import capture_reads
        with capture_reads() as reads:
            prepared = self._prepare_resolution(())
            execution = self._resolve_prepared(prepared)[0]
            review = self._resolve_prepared(prepared, _review=True)[0]
            review = self._with_runtime_requirements(review, execution)
            execution = self._with_runtime_requirements(execution, execution)
        return ReviewSnapshot(self.schema, prepared[6], execution, review,
                              frozenset(prepared[2]), prepared[-1], reads.snapshot(),
                              self._runtime_requirements[0] if self._runtime_requirements else (),
                              self._runtime_requirement_token)

    def _prepare_resolution(self, operation_intents):
        operation_intents = tuple(dict.fromkeys((*self._tool_intents, *operation_intents)))
        registry = self.password_manager_registry
        dotenv, origins = {}, {}
        dotenv_inputs = []
        auth_sources = {'password_manager': 'Not provided', 'roles': {}}
        explicit_manager = None
        auth_error = None
        for path in self.target_paths:
            entries = read_dotenv(path)
            dotenv_inputs.append((path, tuple(entries.items())))
            if 'UNATTENDED_AUTH' in entries:
                try:
                    raw_later = json.loads(entries['UNATTENDED_AUTH'])
                    later = normalize_auth(raw_later, registry=registry)
                    has_manager = ('password_manager' in raw_later
                                   if isinstance(raw_later, dict) and _canonical_shape(raw_later)
                                   else any(raw_later.values()) if isinstance(raw_later, dict) else False)
                    if has_manager:
                        choice = later['password_manager']
                        if explicit_manager is not None and choice != explicit_manager:
                            auth_error = auth_error or 'manager_conflict'
                        explicit_manager = choice
                        auth_sources['password_manager'] = self._source(path)
                    auth_sources['roles'].update({role: self._source(path) for role in later['roles']})
                    if 'UNATTENDED_AUTH' in dotenv:
                        earlier = normalize_auth(json.loads(dotenv['UNATTENDED_AUTH']), registry=registry)
                        later = {'password_manager': later['password_manager'] if has_manager else earlier['password_manager'],
                                 'roles': {**earlier['roles'], **later['roles']}}
                    entries['UNATTENDED_AUTH'] = json.dumps(later, ensure_ascii=False, separators=(',', ':'))
                except (TypeError, ValueError) as exc:
                    kind = ('manager_conflict' if 'different password managers or CLI paths' in str(exc)
                            else 'manager_unavailable' if 'password manager is unavailable' in str(exc)
                            else 'invalid_profile')
                    if auth_error != 'invalid_profile':
                        auth_error = kind
            dotenv.update(entries)
            origins.update({name: self._source(path) for name in entries})
        if auth_error:
            dotenv['UNATTENDED_AUTH'] = json.dumps({'_auth_error': auth_error})
        # Re-evaluate UI choices without inventing duplicate CLI arguments.
        choices = dict(self._choices)
        choices.update(runtime_directory_overrides(self.context))
        self._schema = self._bootstrap_schema(self._provider_base_schema, dotenv, choices, operation_intents)
        return registry, dotenv, origins, auth_sources, choices, operation_intents, self.tool_plan, tuple(dotenv_inputs)

    def _resolve_prepared(self, prepared, *, _cleanup=False, _review=False):
        registry, dotenv, origins, sources, choices, operation_intents, plan, _dotenv_inputs = prepared
        auth_sources, choices = deepcopy(sources), dict(choices)
        argv = self.argv
        schema = self.schema
        # Default review spans modes; execution still uses ordinary applicability.
        # Pack scope remains enforced except in the explicit cleanup inventory.
        # Review inventory spans modes, but conditional requirements retain their
        # effective controller predicates instead of becoming unconditional.
        if _cleanup and plan is not None:
            schema = plan.option_schema
        if _cleanup or _review:
            review_names = frozenset(schema.fields) - (
                set(plan.tool_fields) - set(plan.active_fields) if plan is not None else set())
            schema = ConfigSchema({name: replace(field, applies_when={} if _cleanup else _review_predicate(field.applies_when),
                                                 pack_only=False if _cleanup else field.pack_only)
                                   for name, field in schema.fields.items()
                                   if _cleanup or name in review_names})
        if _cleanup and "HELP" in schema.fields:
            choices['HELP'] = 'False'
        result = resolve_config(schema, argv=argv, environ=self.environ,
                                dotenv=dotenv, anchors=self.context.anchors,
                                interactive=True, pack_launcher=self.context.external_pack,
                                overrides=choices, operation_intents=operation_intents)
        fields = {name: replace(value, source='interactive' if name in choices
                                else origins[name] if value.source == 'dotenv' else value.source)
                  for name, value in result.fields.items()}
        auth = result.fields.get('UNATTENDED_AUTH')
        if auth is not None and auth.source != 'dotenv':
            try:
                raw = auth.value
                normalized = normalize_auth(raw, registry=registry)
                has_manager = ('password_manager' in raw if _canonical_shape(raw) else any(raw.values()))
                source = 'interactive' if 'UNATTENDED_AUTH' in self._choices else auth.source
                auth_sources = {'password_manager': source if has_manager else 'Not provided',
                                'roles': {role: source for role in normalized['roles']}}
            except (TypeError, ValueError):
                auth_sources = {'password_manager': 'Not provided', 'roles': {}}
        result = retain_runtime_directory(replace(result, fields=fields), self.context)
        if plan is not None:
            from .tool_resolution import bind_operation_tools
            result = bind_operation_tools(plan, result, argv=argv)
            self.tool_plan = result.tool_plan
        return result, auth_sources

    def default_candidates(self, *, result=None):
        result = self.result(_review=True) if result is None else result
        return [DefaultCandidate(name, value, self.schema.fields[name])
                for name, value in result.fields.items()
                if not value.explicit
                and self.schema.fields[name].acknowledge_default]

    def set_value(self, name, text, persist=False):
        if name not in self.schema.fields:
            raise ConfigError(f'Unknown configuration field {name}')
        try:
            convert_value(self.schema.fields[name], text, external=True, anchors=self.context.anchors)
        except ValueError as error:
            raise ConfigError(f'{name}: {error}') from None
        if persist:
            self._persist({name: text})
            self._choices.pop(name, None)
        else:
            self._choices[name] = text
        return self.result()

    def external_value(self, name, value):
        """Persist enum tokens rather than anchored display paths."""
        field = self.schema.fields[name]
        if field.type == 'int' and field.allow_empty and value is None:
            return ''
        if field.enum:
            for token in field.enum:
                if convert_value(field, token, external=False, anchors=self.context.anchors) == value:
                    return serialize_value(token)
        return serialize_value(value)

    def save_defaults(self, names):
        candidates = {item.name: item for item in self.default_candidates()}
        names = tuple(dict.fromkeys(names))
        values = {}
        for name in names:
            if name not in candidates:
                raise ConfigError(f'{name}: default cannot be persisted through acknowledgement')
            values[name] = self.external_value(name, candidates[name].value)
        if values:
            self._persist(values)
            for name in values:
                self._choices.pop(name, None)
        return self.result()

    def dotenv_keys(self):
        return DotenvEditor(self.dotenv_path).keys()

    def saved_path(self, name):
        """Find the highest-precedence persisted assignment, independent of target."""
        return next((path for path in reversed(self.target_paths)
                     if name in DotenvEditor(path).keys()), None)

    def unset_saved(self, name):
        if name == 'UNATTENDED_AUTH':
            raise ConfigError('UNATTENDED_AUTH is managed by authentication setup')
        path = self.saved_path(name)
        if path is not None:
            DotenvEditor(path).delete([name])
            self._choices.pop(name, None)
        return self.result()

    def delete_keys(self, names):
        names = tuple(names)
        if 'UNATTENDED_AUTH' in names:
            raise ConfigError('UNATTENDED_AUTH is managed by authentication setup')
        for path in self.target_paths:
            DotenvEditor(path).delete(names)
        for name in names:
            self._choices.pop(name, None)
        return self.result()

    def cleanup_rows(self, *, retained=(), _prepared=None):
        prepared = self._prepare_resolution(()) if _prepared is None else _prepared
        original = self._resolve_prepared(prepared)[0]
        original = self._with_runtime_requirements(original, original)
        result = self._resolve_prepared(prepared, _cleanup=True)[0]
        result = self._with_runtime_requirements(result, original)
        if "HELP" in original.fields:
            result.fields["HELP"] = original.fields["HELP"]
        inputs = {path: dict(entries) for path, entries in prepared[-1]}
        base = inputs[self.context.base_root / '.env']
        pack = inputs[self.context.pack_root / '.env'] if self.context.external_pack else {}
        persisted = set().union(*(set(entries) for entries in inputs.values()))
        inventory = self.tool_plan.option_schema if self.tool_plan is not None else self.schema
        return [CleanupRow(name, result.fields.get(name), field, name in base,
                           name in pack, name in persisted)
                for name, field in sorted(inventory.fields.items())
                if name != 'UNATTENDED_AUTH'
                and (field.acknowledge_default or name in retained
                     or name in persisted)]
