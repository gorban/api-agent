"""Inert declarations and explicit loading for password-manager adapters."""
from dataclasses import dataclass
import importlib.util
import inspect
import os
from pathlib import Path
import re
import sys
import stat
import tomllib
from types import ModuleType
import uuid


class ManagerError(ValueError):
    """A password-manager declaration or implementation needs correction."""


_DIRECTORY_HANDLES = os.open in os.supports_dir_fd


def _implementation_source(path, *, nonregular_message='Password manager implementation must remain a regular Python file'):
    """Read a resolved path without following replaced parent directories."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
    if _DIRECTORY_HANDLES:
        directory = os.open(path.anchor, flags | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(path.name, flags, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ManagerError(nonregular_message)
        return source.read()


@dataclass(frozen=True)
class ManagerDefinition:
    id: str
    label: str
    implementation: Path
    cli_default: str


@dataclass(frozen=True)
class PasswordManagerRegistry:
    definitions: tuple[ManagerDefinition, ...]

    def __post_init__(self):
        object.__setattr__(self, 'definitions', tuple(self.definitions))
        identifiers = [item.id for item in self.definitions]
        if len(set(identifiers)) != len(identifiers):
            raise ManagerError('Conflicting password manager identifiers; remove the duplicate declaration')

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.label, item.id) for item in self.definitions)

    def get(self, identifier: str) -> ManagerDefinition:
        for item in self.definitions:
            if item.id == identifier:
                return item
        raise ManagerError('Selected password manager is unavailable; select an available manager or restore its resource pack')

    def load(self, identifier: str) -> ModuleType:
        definition = self.get(identifier)
        name = '_api_agent_password_manager_' + uuid.uuid4().hex
        try:
            spec = importlib.util.spec_from_file_location(name, definition.implementation)
            if spec is None or spec.loader is None:
                raise ManagerError('Password manager implementation could not be loaded')
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            # Do not follow a retargeted declaration path after discovery.
            if definition.implementation.resolve(strict=True) != definition.implementation:
                raise ManagerError('Password manager implementation path changed; reload its declaration')
            code = compile(_implementation_source(definition.implementation),
                           str(definition.implementation), 'exec')
            exec(code, module.__dict__)
            if type(getattr(module, 'version', None)) is not int or module.version != 1:
                raise ManagerError('Password manager implementation must declare version = 1')
            for hook, arguments in (('command', ('source', 'kind', 'cli')), ('parse', ('output', 'kind'))):
                function = getattr(module, hook, None)
                if (not inspect.isfunction(function) or inspect.iscoroutinefunction(function)
                        or inspect.isasyncgenfunction(function) or inspect.isgeneratorfunction(function)):
                    raise ManagerError(f'Password manager implementation needs synchronous {hook}({", ".join(arguments)})')
                try:
                    inspect.signature(function).bind(*arguments)
                except (TypeError, ValueError):
                    raise ManagerError(f'Password manager implementation has incompatible {hook} arguments') from None
            optional = getattr(module, 'classify_login', None)
            if optional is not None:
                if (not inspect.isfunction(optional) or inspect.iscoroutinefunction(optional)
                        or inspect.isasyncgenfunction(optional) or inspect.isgeneratorfunction(optional)):
                    raise ManagerError('Password manager classification must be synchronous')
                try:
                    inspect.signature(optional).bind('stderr')
                except (TypeError, ValueError):
                    raise ManagerError('Password manager classification has incompatible arguments') from None
            return module
        except ManagerError:
            raise
        except Exception:
            raise ManagerError('Password manager implementation failed to initialize; check its implementation and declaration') from None
        finally:
            sys.modules.pop(name, None)


def _read_definitions(declaration: Path, *, owner_root: Path) -> tuple[ManagerDefinition, ...]:
    declaration = declaration.resolve()
    owner_root = owner_root.resolve()
    if not declaration.is_relative_to(owner_root):
        raise ManagerError("Password manager declaration must remain inside its owning resource pack")
    try:
        data = tomllib.loads(_implementation_source(
            declaration, nonregular_message='Password manager declaration must remain a regular file'
        ).decode('utf-8'))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ManagerError(f'Cannot read password manager declaration: {declaration}') from None
    if set(data) != {'version', 'managers'} or type(data['version']) is not int or data['version'] != 1:
        raise ManagerError(f'Password manager declaration requires version = 1 and managers: {declaration}')
    rows = data['managers']
    if not isinstance(rows, list):
        raise ManagerError(f'Password manager declarations must use [[managers]]: {declaration}')
    definitions = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'label', 'implementation', 'cli_default'}:
            raise ManagerError(f'Password manager declaration requires id, label, implementation and cli_default: {declaration}')
        if not isinstance(row['id'], str) or re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', row['id']) is None:
            raise ManagerError(f'Password manager id must be a lowercase stable identifier: {declaration}')
        for key in ('label', 'cli_default'):
            if not isinstance(row[key], str) or not row[key].strip() or row[key] != row[key].strip() or any(ord(char) < 32 for char in row[key]):
                raise ManagerError(f'Password manager {key} must be nonempty printable text: {declaration}')
        location = row['implementation']
        if not isinstance(location, str) or not location.strip() or Path(location).is_absolute():
            raise ManagerError(f'Password manager implementation must be a relative .py path: {declaration}')
        implementation = (declaration.parent / location).resolve()
        if not implementation.is_relative_to(owner_root):
            raise ManagerError('Password manager implementation must remain inside its owning resource pack')
        if implementation.suffix != '.py' or not implementation.is_file():
            raise ManagerError(f'Password manager implementation must name an existing .py file: {declaration}')
        definitions.append(ManagerDefinition(row['id'], row['label'], implementation, row['cli_default']))
    return tuple(definitions)


def bundled_registry() -> PasswordManagerRegistry:
    return PasswordManagerRegistry(_read_definitions(Path(__file__).parent / 'password_managers/cfg/managers.toml', owner_root=Path(__file__).parent / 'password_managers'))


def load_manager_registry(*, pack_root=None) -> PasswordManagerRegistry:
    definitions = bundled_registry().definitions
    if pack_root is not None:
        declaration = Path(pack_root) / 'cfg/password_managers.toml'
        if declaration.exists() or declaration.is_symlink():
            definitions += _read_definitions(declaration, owner_root=Path(pack_root))
    return PasswordManagerRegistry(definitions)


def registry_for_context(context) -> PasswordManagerRegistry:
    return load_manager_registry(pack_root=context.pack_root if getattr(context, "external_pack", False) else None)


def registry_for_owner(owner_root) -> PasswordManagerRegistry:
    if owner_root is None:
        return bundled_registry()
    owner = Path(owner_root).resolve()
    package = Path(__file__).resolve().parents[1]
    base = package.parents[1] if package.parent.name == 'src' else package
    return load_manager_registry(pack_root=None if owner == base else owner)
