"""Editable launch configuration retaining original external provenance."""
from dataclasses import dataclass, replace
import os
from pathlib import Path
import tempfile

from .declarations import ConfigSchema, FieldDeclaration
from .dotenv import read_dotenv
from .editor import DotenvEditor, serialize_value
from .resolve import ConfigError, ResolvedValue, convert_value, resolve_config

_CONTROLS = {'HELP', 'TUI_MODE', 'UNATTENDED_AUTH'}


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


class ConfigSession:
    def __init__(self, context, schema, argv=(), environ=None):
        self.context = context
        self.schema = schema
        self.argv = tuple(argv)
        self.environ = dict(os.environ if environ is None else environ)
        self._choices = {}
        launcher = getattr(context, 'launcher_root', None)
        self._dotenv_path = Path(launcher) / '.env' if launcher else Path(context.dotenv_path)

    @property
    def dotenv_path(self):
        return self._dotenv_path

    @property
    def target_paths(self):
        base = self.context.base_root / '.env'
        pack = self.context.pack_root / '.env'
        launcher = getattr(self.context, 'launcher_root', None) or self.context.base_root
        paths = [base, Path(launcher) / '.env']
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
            path.unlink()
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

    def result(self, *, _cleanup=False, _review=False):
        dotenv, origins = {}, {}
        for path in self.target_paths:
            entries = read_dotenv(path)
            dotenv.update(entries)
            origins.update({name: self._source(path) for name in entries})
        # Put choices last as CLI options to re-evaluate conditional defaults and
        # applicability using the ordinary resolver, then restore UI provenance.
        argv = list(self.argv)
        boundary = argv.index('--') if '--' in argv else len(argv)
        argv[boundary:boundary] = [f'--{name}={text}' for name, text in self._choices.items()]
        schema = self.schema
        # Default review spans modes; execution still uses ordinary applicability.
        # Pack scope remains enforced except in the explicit cleanup inventory.
        if _cleanup or _review:
            schema = ConfigSchema({name: replace(field, applies_when={},
                                                 pack_only=False if _cleanup else field.pack_only,
                                                 required=(field.required or bool(field.required_when)) if _review else field.required,
                                                 required_when={} if _review else field.required_when)
                                   for name, field in self.schema.fields.items()})
            if _cleanup and "HELP" in schema.fields:
                boundary = argv.index("--") if "--" in argv else len(argv)
                argv.insert(boundary, "--help=False")
        result = resolve_config(schema, argv=argv, environ=self.environ,
                                dotenv=dotenv, anchors=self.context.anchors,
                                interactive=True, pack_launcher=self.context.external_pack)
        fields = {name: replace(value, source='interactive' if name in self._choices
                                else origins[name] if value.source == 'dotenv' else value.source)
                  for name, value in result.fields.items()}
        return replace(result, fields=fields)

    def default_candidates(self):
        result = self.result(_review=True)
        return [DefaultCandidate(name, value, self.schema.fields[name])
                for name, value in result.fields.items()
                if not value.explicit and name not in _CONTROLS
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

    def save_defaults(self, names):
        candidates = {item.name: item for item in self.default_candidates()}
        names = tuple(dict.fromkeys(names))
        values = {}
        for name in names:
            if name not in candidates or not candidates[name].field.persist_default:
                raise ConfigError(f'{name}: default cannot be persisted through acknowledgement')
            values[name] = serialize_value(candidates[name].value)
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

    def cleanup_rows(self):
        original = self.result()
        result = self.result(_cleanup=True)
        if "HELP" in original.fields:
            result.fields["HELP"] = original.fields["HELP"]
        base = read_dotenv(self.context.base_root / '.env')
        pack = read_dotenv(self.context.pack_root / '.env') if self.context.external_pack else {}
        persisted = set().union(*(DotenvEditor(path).keys() for path in self.target_paths))
        return [CleanupRow(name, result.fields.get(name), field, name in base,
                           name in pack, name in persisted)
                for name, field in self.schema.fields.items() if name != 'UNATTENDED_AUTH']
