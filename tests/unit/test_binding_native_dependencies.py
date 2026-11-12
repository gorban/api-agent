"""The binding grammar serves its native sources' static local import closure from admitted bytes."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from test_workflow_definition import make_graph

from api_agent.runtime.local_tools import selected_application
from api_agent.workflows.definition import load_definition
from api_agent.workflows.layout import LayoutError

SRC = Path(__file__).resolve().parents[2] / 'src'
APPLICATION = '''## Application

| Registry | Source |
| --- | --- |
| create_application | [Python](<resource_pack>/application.py) |

## Flow'''


def layered_graph(tmp_path, package):
    """A library root owning the graph and a namespace package; the selecting pack owns the factory."""
    library, owner = tmp_path.resolve() / 'library', tmp_path.resolve() / 'owner'
    library.mkdir()
    owner.mkdir()
    entry = make_graph(library)
    entry.write_text(entry.read_text().replace('## Flow', APPLICATION))
    shared = library / package / 'shared'
    shared.mkdir(parents=True)
    (shared / '__init__.py').write_text('')
    (shared / 'application.py').write_text(f'''from types import SimpleNamespace

def build_application(definition, config):
    async def complete(request, *, llm, context):
        from {package}.shared.lazy import answer
        return answer(request)
    return SimpleNamespace(definition=definition, config=config, complete=complete,
                           timeouts={{}}, requires_api=False)
''')
    (shared / 'lazy.py').write_text("def answer(request):\n    return 'admitted ' + request\n")
    (owner / 'application.py').write_text(f'''import json
import api_agent.workflows.model
from {package}.shared.application import build_application

def create_application(definition, *, config):
    return build_application(definition, config)
''')
    return library, owner, entry


def load(library, owner, entry):
    return load_definition(entry, base_root=library, pack_root=owner)


def forget(package):
    for name in [name for name in sys.modules if name == package or name.startswith(package + '.')]:
        del sys.modules[name]


def test_layered_import_loads_in_a_fresh_interpreter_without_the_library_on_the_path(tmp_path):
    package = 'layered_fresh_library'
    library, owner, entry = layered_graph(tmp_path, package)
    command = '''import asyncio, sys
from pathlib import Path
from api_agent.runtime.local_tools import selected_application
from api_agent.workflows.definition import load_definition
library, owner, entry = map(Path, sys.argv[1:4])
assert not any(Path(item or '.').resolve() in (library, owner) for item in sys.path)
definition = load_definition(entry, base_root=library, pack_root=owner)
application = selected_application(definition)
print(asyncio.run(application.complete('turn', llm=None, context=None)))
'''
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    completed = subprocess.run(
        [sys.executable, '-c', command, str(library), str(owner), str(entry)],
        cwd=tmp_path, env={**environment, 'PYTHONPATH': str(SRC)},
        capture_output=True, text=True, timeout=60)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == 'admitted turn'


def test_layered_closure_is_admitted_and_served_from_admitted_bytes(tmp_path):
    package = 'layered_inprocess_library'
    library, owner, entry = layered_graph(tmp_path, package)
    shared = library / package / 'shared'
    assert not any(Path(item or '.').resolve() in (library, owner) for item in sys.path)
    try:
        definition = load(library, owner, entry)
        application = selected_application(definition)
        answer = asyncio.run(application.complete('turn', llm=None, context=None))
        assert sys.modules[package].__path__ == [str(library / package)]
        assert sys.modules[package + '.shared.lazy'].__file__ == str(shared / 'lazy.py')
    finally:
        forget(package)

    assert answer == 'admitted turn'
    assert dict(definition.native_dependencies) == {
        package + '.shared': shared / '__init__.py',
        package + '.shared.application': shared / 'application.py',
        package + '.shared.lazy': shared / 'lazy.py'}
    assert {item.logical_path for item in definition.resources if item.kind == 'native_dependency'} == {
        f'base/{package}/shared/{name}' for name in ('__init__.py', 'application.py', 'lazy.py')}


def test_dependency_bytes_are_part_of_the_definition_identity(tmp_path):
    library, owner, entry = layered_graph(tmp_path, 'layered_identity_library')
    first = load(library, owner, entry)
    (library / 'layered_identity_library' / 'shared' / 'lazy.py').write_text(
        "def answer(request):\n    return 'revised ' + request\n")

    assert load(library, owner, entry).definition_id != first.definition_id


def test_tampered_dependency_is_rejected_before_execution(tmp_path):
    package = 'layered_tampered_library'
    library, owner, entry = layered_graph(tmp_path, package)
    definition = load(library, owner, entry)
    (library / package / 'shared' / 'application.py').write_text(
        "raise RuntimeError('tampered dependency was executed')\n")

    with pytest.raises(LayoutError, match='application.py:1: native source changed after definition admission'):
        selected_application(definition)
    assert package not in sys.modules


def test_local_module_created_after_admission_is_rejected(tmp_path):
    package = 'layered_unadmitted_library'
    library, owner, entry = layered_graph(tmp_path, package)
    factory = owner / 'application.py'
    factory.write_text(f'try:\n    import {package}_late\nexcept ImportError:\n    pass\n' + factory.read_text())
    definition = load(library, owner, entry)
    (owner / f'{package}_late.py').write_text('')

    with pytest.raises(LayoutError, match=f'{package}_late resolves outside the admitted dependency closure'):
        definition.application_factory()


@pytest.mark.parametrize('origin', ['missing', 'different', 'none'])
def test_module_already_imported_from_other_bytes_is_rejected(tmp_path, origin):
    package = f'layered_shadowed_{origin}_library'
    library, owner, entry = layered_graph(tmp_path, package)
    definition = load(library, owner, entry)
    shadow = ModuleType(package + '.shared')
    if origin != 'none':
        shadow.__file__ = str(tmp_path / 'elsewhere.py')
    if origin == 'different':
        (tmp_path / 'elsewhere.py').write_text('# another module\n')
    sys.modules[package + '.shared'] = shadow
    try:
        with pytest.raises(LayoutError, match='already loaded from another source; restart required'):
            definition.application_factory()
    finally:
        forget(package)


@pytest.mark.parametrize('copied', [False, True])
def test_module_already_imported_from_the_admitted_bytes_is_reused(tmp_path, copied):
    import importlib
    import shutil
    package = f'layered_preloaded_{"copy" if copied else "same"}_library'
    library, owner, entry = layered_graph(tmp_path, package)
    definition = load(library, owner, entry)
    installed = library
    if copied:
        installed = tmp_path.resolve() / 'installed'
        shutil.copytree(library / package, installed / package)
    sys.path.insert(0, str(installed))
    try:
        preloaded = importlib.import_module(package + '.shared.application')
        sys.path.remove(str(installed))
        factory = definition.application_factory()
        assert factory.__globals__['build_application'] is preloaded.build_application
        assert preloaded.__file__ == str(installed / package / 'shared' / 'application.py')
    finally:
        if str(installed) in sys.path:
            sys.path.remove(str(installed))
        forget(package)


def test_definitions_with_identical_dependency_bytes_share_modules(tmp_path):
    package = 'layered_shared_library'
    library, owner, entry = layered_graph(tmp_path, package)
    other = owner.parent / 'other_owner'
    other.mkdir()
    (other / 'application.py').write_text((owner / 'application.py').read_text() + '# another factory\n')
    first, second = load(library, owner, entry), load(library, other, entry)
    try:
        shared = first.application_factory().__globals__['build_application']
        assert second.definition_id != first.definition_id
        assert second.application_factory().__globals__['build_application'] is shared
        (library / package / 'shared' / 'lazy.py').write_text("def answer(request):\n    return request\n")
        with pytest.raises(LayoutError, match='lazy.py:1: native dependency already loaded from another source'):
            load(library, other, entry).application_factory()
    finally:
        forget(package)


def test_native_source_imported_as_a_module_is_scanned_once(tmp_path):
    package = 'layered_reimported_library'
    library, owner, entry = layered_graph(tmp_path, package)
    factory = owner / 'application.py'
    factory.write_text(factory.read_text().replace('import json\n', 'import json\nimport application\n'))

    definition = load(library, owner, entry)

    assert definition.native_dependencies['application'] == factory
    assert {item.kind for item in definition.resources if item.logical_path == 'pack/application.py'} == {
        'native_source'}
