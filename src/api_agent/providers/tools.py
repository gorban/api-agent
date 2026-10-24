"""Small API-independent contract for one selected read-only tool."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Literal, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012


MAX_SCHEMA_BYTES = 65536
MAX_ARGUMENT_BYTES = 16384
MAX_RESULT_BYTES = 262144


class ToolValidationError(ValueError):
    """A candidate rejected deterministically before any execution."""


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


TOOL_SCHEMA_REGISTRY = Registry(retrieve=_no_remote)


def _local_references(schema):
    pending = [schema]
    while pending:
        value = pending.pop()
        if type(value) is dict:
            if '$id' in value or '$dynamicRef' in value or '$recursiveRef' in value:
                raise ValueError('Tool schema contains an unsupported reference')
            if '$ref' in value and (type(value['$ref']) is not str
                                    or not value['$ref'].startswith('#/')):
                raise ValueError('Tool schema contains a nonlocal reference')
            pending.extend(DRAFT202012.subresources_of(value))


def canonical(value, limit):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > limit or json.loads(encoded) != value:
            raise ValueError
    except (TypeError, ValueError, RecursionError):
        raise ValueError('Tool value is not bounded JSON') from None
    return encoded


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = field(repr=False)
    input_schema: dict = field(repr=False)
    output_schema: dict = field(repr=False)
    schema_fingerprint: str

    def __post_init__(self):
        if (not isinstance(self.name, str) or not self.name or len(self.name) > 100
                or not self.name.isprintable() or not isinstance(self.description, str)
                or not self.description or len(self.description) > 1000
                or not isinstance(self.schema_fingerprint, str) or len(self.schema_fingerprint) != 64
                or any(c not in '0123456789abcdef' for c in self.schema_fingerprint)):
            raise ValueError('Invalid tool specification')
        for schema in (self.input_schema, self.output_schema):
            if type(schema) is not dict:
                raise ValueError('Invalid tool schema')
            canonical(schema, MAX_SCHEMA_BYTES)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError('Invalid tool schema') from error
            _local_references(schema)

    @property
    def digest(self):
        content = [self.name, self.description, self.input_schema,
                   self.output_schema, self.schema_fingerprint]
        return sha256(canonical(content, MAX_SCHEMA_BYTES * 2).encode()).hexdigest()


@dataclass(frozen=True)
class PreparedCall:
    spec: ToolSpec
    arguments: dict = field(repr=False)
    invocation_id: str
    digest: str


@dataclass(frozen=True)
class ToolResult:
    status: Literal['success', 'partial', 'failure']
    data: object = field(repr=False)
    errors: tuple = field(default=(), repr=False)
    diagnostic: str | None = None

    def __post_init__(self):
        if self.status not in ('success', 'partial', 'failure') or type(self.errors) is not tuple:
            raise ValueError('Invalid tool result')
        if self.status in ('success', 'partial') and self.data is None:
            raise ValueError('Tool result is missing data')
        if self.diagnostic is not None and (type(self.diagnostic) is not str
                                            or len(self.diagnostic) > 256
                                            or not self.diagnostic.isprintable()):
            raise ValueError('Invalid tool diagnostic')
        canonical({'data': self.data, 'errors': list(self.errors)}, MAX_RESULT_BYTES)


class ToolOwner(Protocol):
    async def describe(self) -> ToolSpec: ...
    async def validate(self, prepared: PreparedCall) -> None: ...
    async def invoke(self, prepared: PreparedCall) -> ToolResult: ...
    async def aclose(self) -> None: ...
