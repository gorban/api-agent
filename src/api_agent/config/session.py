"""Editable launch configuration retaining original external provenance."""
from dataclasses import dataclass, replace
import os

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

    @property
    def dotenv_path(self):
        return self.context.dotenv_path

    def result(self, *, _cleanup=False):
        dotenv = {**read_dotenv(self.context.base_root / '.env'),
                  **read_dotenv(self.dotenv_path)}
        # Put choices last as CLI options to re-evaluate conditional defaults and
        # applicability using the ordinary resolver, then restore UI provenance.
        argv = list(self.argv)
        boundary = argv.index('--') if '--' in argv else len(argv)
        argv[boundary:boundary] = [f'--{name}={text}' for name, text in self._choices.items()]
        schema = self.schema
        if _cleanup:
            schema = ConfigSchema({name: replace(field, applies_when={}, pack_only=False)
                                   for name, field in self.schema.fields.items()})
            if "HELP" in schema.fields:
                boundary = argv.index("--") if "--" in argv else len(argv)
                argv.insert(boundary, "--help=False")
        result = resolve_config(schema, argv=argv, environ=self.environ,
                                dotenv=dotenv, anchors=self.context.anchors,
                                interactive=True, pack_launcher=self.context.external_pack)
        fields = {name: replace(value, source='interactive') if name in self._choices else value
                  for name, value in result.fields.items()}
        return replace(result, fields=fields)

    def default_candidates(self):
        result = self.result()
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
            DotenvEditor(self.dotenv_path).update({name: text})
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
            DotenvEditor(self.dotenv_path).update(values)
        return self.result()

    def dotenv_keys(self):
        return DotenvEditor(self.dotenv_path).keys()

    def delete_keys(self, names):
        names = tuple(names)
        if 'UNATTENDED_AUTH' in names:
            raise ConfigError('UNATTENDED_AUTH is managed by authentication setup')
        DotenvEditor(self.dotenv_path).delete(names)
        return self.result()

    def cleanup_rows(self):
        original = self.result()
        result = self.result(_cleanup=True)
        if "HELP" in original.fields:
            result.fields["HELP"] = original.fields["HELP"]
        base = read_dotenv(self.context.base_root / '.env')
        pack = read_dotenv(self.dotenv_path)
        return [CleanupRow(name, result.fields.get(name), field, name in base,
                           name in pack, name in pack)
                for name, field in self.schema.fields.items() if name != 'UNATTENDED_AUTH']
