"""Declaration discovery stays inert; only retrieval imports adapter code."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from api_agent.auth import managers


VALID = 'version = 1\ndef command(source, kind, cli): return [cli, kind]\ndef parse(output, kind): return output\n'


def declaration(root, *, identifier='third', implementation='../adapter.py', extra='', version='1'):
    cfg = root / 'cfg'
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / 'password_managers.toml'
    path.write_text(f'version = {version}\n[[managers]]\nid = "{identifier}"\nlabel = "Third manager"\nimplementation = "{implementation}"\ncli_default = "third-cli"\n{extra}')
    return path


def test_pack_discovery_and_loading_are_separate(tmp_path):
    marker = tmp_path / 'imported'
    (tmp_path / 'adapter.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n' + VALID)
    declaration(tmp_path)
    registry = managers.load_manager_registry(pack_root=tmp_path)
    assert registry.choices == (('1Password', '1password'), ('Keeper', 'keeper'), ('Third manager', 'third'))
    assert not marker.exists()
    definition = registry.get('third')
    assert definition.implementation == tmp_path / 'adapter.py'
    assert definition.cli_default == 'third-cli'
    with pytest.raises(FrozenInstanceError):
        definition.label = 'changed'
    adapter = registry.load('third')
    assert marker.exists()
    assert adapter.command(None, 'password', 'override') == ['override', 'password']
    assert adapter.parse('value', 'password') == 'value'
    assert adapter.__name__ not in sys.modules
    with pytest.raises(managers.ManagerError, match='unavailable'):
        managers.bundled_registry().get('third')


@pytest.mark.parametrize('change, match', [
    ({'version': 'true'}, 'version'), ({'version': '2'}, 'version'),
    ({'identifier': 'Bad'}, 'identifier'), ({'implementation': '/absolute.py'}, 'relative'),
    ({'implementation': '../missing.py'}, 'existing'), ({'extra': 'unexpected = 1'}, 'requires id'),
])
def test_invalid_declarations(tmp_path, change, match):
    (tmp_path / 'adapter.py').write_text(VALID)
    declaration(tmp_path, **change)
    with pytest.raises(managers.ManagerError, match=match):
        managers.load_manager_registry(pack_root=tmp_path)


def test_conflict_and_malformed(tmp_path):
    (tmp_path / 'adapter.py').write_text(VALID)
    path = declaration(tmp_path, identifier='keeper')
    with pytest.raises(managers.ManagerError, match='Conflicting'):
        managers.load_manager_registry(pack_root=tmp_path)
    path.write_text('not valid toml')
    with pytest.raises(managers.ManagerError, match='Cannot read'):
        managers.load_manager_registry(pack_root=tmp_path)


@pytest.mark.parametrize('code, match', [
    ('version=True\n' + VALID.split('\n', 1)[1], 'version'),
    ('version=1\ncommand=1\ndef parse(output,kind): pass\n', 'synchronous command'),
    ('version=1\nasync def command(source,kind,cli): pass\ndef parse(output,kind): pass\n', 'synchronous command'),
    ('version=1\ndef command(source,kind): pass\ndef parse(output,kind): pass\n', 'incompatible command'),
    ('version=1\ndef command(source,kind,cli): pass\ndef parse(*,output,kind): pass\n', 'incompatible parse'),
    ('raise RuntimeError("private detail")', 'failed to initialize'),
])
def test_adapter_contract_before_helper(tmp_path, code, match):
    (tmp_path / 'adapter.py').write_text(code)
    declaration(tmp_path)
    registry = managers.load_manager_registry(pack_root=tmp_path)
    before = set(sys.modules)
    with pytest.raises(managers.ManagerError, match=match):
        registry.load('third')
    assert not (set(sys.modules) - before) & {name for name in sys.modules if name.startswith('_api_agent_password_manager_')}


def test_declaration_symlink_uses_actual_location(tmp_path):
    selected = tmp_path / 'selected'
    owning = selected / 'owning'
    owning.mkdir(parents=True)
    (owning / 'adapter.py').write_text(VALID)
    target = declaration(owning)
    (selected / 'cfg').mkdir(parents=True)
    (selected / 'cfg/password_managers.toml').symlink_to(target)
    assert managers.load_manager_registry(pack_root=selected).get('third').implementation == owning / 'adapter.py'


def test_context_owner_missing_extension_and_fresh_load(tmp_path):
    assert managers.load_manager_registry(pack_root=tmp_path).choices == managers.bundled_registry().choices
    (tmp_path / 'adapter.py').write_text(VALID)
    declaration(tmp_path)
    context = SimpleNamespace(external_pack=False, pack_root=tmp_path)
    assert len(managers.registry_for_context(context).choices) == 2
    context.external_pack = True
    assert len(managers.registry_for_context(context).choices) == 3
    assert len(managers.registry_for_owner(tmp_path).choices) == 3
    assert len(managers.registry_for_owner(None).choices) == 2
    registry = managers.load_manager_registry(pack_root=tmp_path)
    first, second = registry.load('third'), registry.load('third')
    assert first is not second and first.__name__ != second.__name__


@pytest.mark.parametrize('body, match', [
    ('version=1\nmanagers=3', 'use'),
    ('version=1\n[[managers]]\nid="third"', 'requires id'),
    ('version=1\n[[managers]]\nid="third"\nlabel=""\nimplementation="../adapter.py"\ncli_default="cli"', 'label'),
    ('version=1\n[[managers]]\nid="third"\nlabel="Third"\nimplementation="../adapter.py"\ncli_default=" cli"', 'cli_default'),
])
def test_strict_declaration_fields(tmp_path, body, match):
    (tmp_path / 'adapter.py').write_text(VALID)
    path = declaration(tmp_path)
    path.write_text(body)
    with pytest.raises(managers.ManagerError, match=match):
        managers.load_manager_registry(pack_root=tmp_path)


def test_source_owner_does_not_read_base_extension(tmp_path, monkeypatch):
    package = tmp_path / 'src/api_agent'
    (package / 'auth').mkdir(parents=True)
    monkeypatch.setattr(managers, '__file__', str(package / 'auth/managers.py'))
    registry = managers.PasswordManagerRegistry(())
    calls = []
    monkeypatch.setattr(managers, 'load_manager_registry', lambda **kwargs: calls.append(kwargs) or registry)
    assert managers.registry_for_owner(tmp_path) is registry
    assert calls == [{'pack_root': None}]


def test_packaged_owner_does_not_read_base_extension(tmp_path, monkeypatch):
    package = tmp_path / 'api_agent'
    (package / 'auth').mkdir(parents=True)
    monkeypatch.setattr(managers, '__file__', str(package / 'auth/managers.py'))
    calls = []
    monkeypatch.setattr(managers, 'load_manager_registry', lambda **kwargs: calls.append(kwargs))
    managers.registry_for_owner(package)
    assert calls == [{'pack_root': None}]


def test_same_basename_pack_modules_do_not_collide(tmp_path):
    registries = []
    for index in range(2):
        root = tmp_path / str(index)
        root.mkdir()
        (root / 'adapter.py').write_text(VALID + f'value = {index}\n')
        declaration(root)
        registries.append(managers.load_manager_registry(pack_root=root))
    assert [registry.load('third').value for registry in registries] == [0, 1]


def test_reload_reads_current_source_without_stale_bytecode(tmp_path):
    path = tmp_path / 'adapter.py'
    path.write_text(VALID + 'value = 1\n')
    declaration(tmp_path)
    registry = managers.load_manager_registry(pack_root=tmp_path)
    assert registry.load('third').value == 1
    path.write_text(VALID + 'value = 2\n')
    assert registry.load('third').value == 2


def test_existing_non_python_file_is_rejected(tmp_path):
    (tmp_path / 'adapter.txt').write_text(VALID)
    declaration(tmp_path, implementation='../adapter.txt')
    with pytest.raises(managers.ManagerError, match='existing .py'):
        managers.load_manager_registry(pack_root=tmp_path)


def test_broken_declaration_symlink_is_rejected(tmp_path):
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/password_managers.toml').symlink_to(tmp_path / 'missing.toml')
    with pytest.raises(managers.ManagerError, match='Cannot read'):
        managers.load_manager_registry(pack_root=tmp_path)


def test_immutable_registry_normalizes_input():
    definitions = [managers.ManagerDefinition('example', 'Example', Path('/example.py'), 'example')]
    registry = managers.PasswordManagerRegistry(definitions)
    definitions.clear()
    assert registry.choices == (('Example', 'example'),)
    with pytest.raises(FrozenInstanceError):
        registry.definitions = ()


@pytest.mark.parametrize('hook', ['command', 'parse'])
def test_generator_hooks_rejected_before_helper(tmp_path, hook):
    code = VALID.replace('return [cli, kind]', 'yield cli') if hook == 'command' else VALID.replace('return output', 'yield output')
    (tmp_path / 'adapter.py').write_text(code)
    declaration(tmp_path)
    with pytest.raises(managers.ManagerError, match=f'synchronous {hook}'):
        managers.load_manager_registry(pack_root=tmp_path).load('third')


def test_minimal_context_uses_bundled_registry():
    assert managers.registry_for_context(SimpleNamespace()).choices == managers.bundled_registry().choices


def test_missing_import_spec_is_actionable(tmp_path, monkeypatch):
    (tmp_path / 'adapter.py').write_text(VALID)
    declaration(tmp_path)
    registry = managers.load_manager_registry(pack_root=tmp_path)
    monkeypatch.setattr(managers.importlib.util, 'spec_from_file_location', lambda *args: None)
    with pytest.raises(managers.ManagerError, match='could not be loaded'):
        registry.load('third')


def test_external_declaration_symlink_rejected(tmp_path):
    owner = tmp_path / 'owner'
    owner.mkdir()
    (owner / 'adapter.py').write_text(VALID)
    target = declaration(owner)
    selected = tmp_path / 'selected'
    (selected / 'cfg').mkdir(parents=True)
    (selected / 'cfg/password_managers.toml').symlink_to(target)
    with pytest.raises(managers.ManagerError, match='declaration must remain'):
        managers.load_manager_registry(pack_root=selected)


@pytest.mark.parametrize('symlink', [False, True])
def test_implementation_cannot_escape_selected_pack(tmp_path, symlink):
    (tmp_path / 'outside.py').write_text(VALID)
    selected = tmp_path / 'selected'
    selected.mkdir()
    if symlink:
        (selected / 'adapter.py').symlink_to(tmp_path / 'outside.py')
    declaration(selected, implementation='../adapter.py' if symlink else '../../outside.py')
    with pytest.raises(managers.ManagerError, match='implementation must remain'):
        managers.load_manager_registry(pack_root=selected)


def test_symlink_replacement_after_discovery_cannot_execute(tmp_path):
    selected = tmp_path / 'selected'
    selected.mkdir()
    adapter = selected / 'adapter.py'
    adapter.write_text(VALID)
    declaration(selected)
    registry = managers.load_manager_registry(pack_root=selected)
    marker = tmp_path / 'outside-executed'
    outside = tmp_path / 'outside.py'
    outside.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n' + VALID)
    adapter.unlink()
    adapter.symlink_to(outside)
    with pytest.raises(managers.ManagerError, match='path changed'):
        registry.load('third')
    assert not marker.exists()


@pytest.mark.parametrize('hook', ['command', 'parse'])
def test_async_callable_objects_are_not_synchronous_hooks(tmp_path, hook):
    code = VALID + f'class AsyncHook:\n    async def __call__(self, *args): return None\n{hook} = AsyncHook()\n'
    (tmp_path / 'adapter.py').write_text(code)
    declaration(tmp_path)
    with pytest.raises(managers.ManagerError, match=f'synchronous {hook}'):
        managers.load_manager_registry(pack_root=tmp_path).load('third')


def test_replaced_nonregular_implementation_is_rejected(tmp_path, monkeypatch):
    (tmp_path / 'adapter.py').write_text(VALID)
    declaration(tmp_path)
    registry = managers.load_manager_registry(pack_root=tmp_path)
    monkeypatch.setattr(managers.os, 'fstat', lambda fd: SimpleNamespace(st_mode=0))
    with pytest.raises(managers.ManagerError, match='regular Python file'):
        registry.load('third')


def test_parent_directory_swap_during_open_cannot_execute(tmp_path, monkeypatch):
    selected = tmp_path / 'selected'
    implementations = selected / 'impl'
    implementations.mkdir(parents=True)
    (implementations / 'adapter.py').write_text(VALID)
    declaration(selected, implementation='../impl/adapter.py')
    registry = managers.load_manager_registry(pack_root=selected)
    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = tmp_path / 'outside-executed'
    (outside / 'adapter.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n' + VALID)
    actual_open = managers.os.open
    swapped = False
    def open_after_swap(path, flags, **kwargs):
        nonlocal swapped
        if not swapped:
            implementations.rename(selected / 'original-impl')
            implementations.symlink_to(outside, target_is_directory=True)
            swapped = True
        return actual_open(path, flags, **kwargs)
    monkeypatch.setattr(managers.os, 'open', open_after_swap)
    with pytest.raises(managers.ManagerError, match='failed to initialize'):
        registry.load('third')
    assert swapped and not marker.exists()


def test_non_directory_handle_platform_loads_regular_file(tmp_path, monkeypatch):
    (tmp_path / 'adapter.py').write_text(VALID)
    declaration(tmp_path)
    registry = managers.load_manager_registry(pack_root=tmp_path)
    monkeypatch.setattr(managers, '_DIRECTORY_HANDLES', False)
    assert registry.load('third').parse('value', 'password') == 'value'


@pytest.mark.parametrize('symlink', [False, True])
def test_fifo_declaration_fails_without_blocking_discovery(tmp_path, symlink):
    import os
    import threading
    from api_agent.config.session import ConfigSession
    path = declaration(tmp_path)
    path.unlink()
    fifo = tmp_path / 'declaration-fifo' if symlink else path
    os.mkfifo(fifo)
    if symlink:
        path.symlink_to(fifo)
    session = ConfigSession.__new__(ConfigSession)
    session.context = SimpleNamespace(external_pack=True, pack_root=tmp_path)
    errors = []
    def discover():
        try:
            session.password_manager_registry
        except managers.ManagerError as error:
            errors.append(str(error))
    worker = threading.Thread(target=discover)
    worker.start()
    try:
        worker.join(1)
        assert not worker.is_alive(), 'FIFO declaration blocked configuration discovery'
        assert errors == ['Password manager declaration must remain a regular file']
    finally:
        # Release a regressed blocking reader so the proof cannot leak a thread.
        if worker.is_alive():
            descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(descriptor, b'version = 1\nmanagers = []\n')
            finally:
                os.close(descriptor)
        worker.join(1)
        assert not worker.is_alive()
    fifo.unlink()
    if symlink:
        path.unlink()
    (tmp_path / 'adapter.py').write_text(VALID)
    declaration(tmp_path)
    assert session.password_manager_registry.get('third').cli_default == 'third-cli'
