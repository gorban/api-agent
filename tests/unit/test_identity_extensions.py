"""Pack registration and composition contracts using synthetic adapter code."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api_agent.auth.extensions import IdentityExtensionError, load_identity_adapter
from api_agent.auth.identity import IdentityError, VerifiedPrincipal
from api_agent.api.composition import compose_runtime
from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
from api_agent.testing.runner import _production_sources


ADAPTER = '''
class Adapter:
    version = 1
    async def resolve_request(self, **kwargs): return None
    async def login_hint(self, principal): return None
    async def match_llm_identity(self, principal, llm_auth_context): return False

def create(config):
    assert config['configured'] == 'yes'
    return Adapter()
'''


def pack(tmp_path, source=ADAPTER, identity=True, production='["."]'):
    base = tmp_path / 'base'
    base.mkdir(exist_ok=True)
    root = tmp_path / 'pack'
    root.mkdir(exist_ok=True)
    (root / 'cfg.toml').write_text('version=1\n')
    (root / 'auth.py').write_text(source)
    text = f'version=1\nid="synthetic"\nconfig="cfg.toml"\nproduction={production}\n'
    if identity:
        text += '[authentication.identity]\nversion=1\npath="auth.py"\nfactory="create"\n'
    (root / 'pack.toml').write_text(text)
    return load_launch_context(base, root)


def test_optional_factory_loads_only_at_composition_and_leaves_no_global_import(tmp_path):
    context = pack(tmp_path)
    assert context.identity_factory_path == context.pack_root / 'auth.py'
    before_modules = set(sys.modules)
    before = list(sys.path)
    first = load_identity_adapter(context, {'configured': 'yes'})
    second = load_identity_adapter(context, {'configured': 'yes'})
    assert first.version == second.version == 1 and type(first) is not type(second)
    assert set(sys.modules) == before_modules and sys.path == before


def test_concurrent_composition_isolates_import_time_module_registration(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from importlib.machinery import SourceFileLoader
    from threading import Barrier
    context = pack(tmp_path, 'from dataclasses import dataclass\n@dataclass\n' + ADAPTER.lstrip())
    barrier = Barrier(2)
    original = SourceFileLoader.exec_module
    def execute(loader, module):
        barrier.wait(timeout=5)
        assert sys.modules[module.__name__] is module
        return original(loader, module)
    before = set(sys.modules)
    with patch.object(SourceFileLoader, 'exec_module', execute), ThreadPoolExecutor(2) as pool:
        adapters = list(pool.map(lambda _: load_identity_adapter(context, {'configured': 'yes'}), range(2)))
    assert type(adapters[0]) is not type(adapters[1])
    assert set(sys.modules) == before


def test_standalone_has_no_jwt_requirement_and_shutdown_clears_owned_sessions(tmp_path):
    context = pack(tmp_path, identity=False)
    runtime = compose_runtime(context, {}, callback_origin='https://agent.example:4206')
    assert runtime.identity_adapter is None
    runtime.start()
    opened = runtime.sessions.open(None, None, create=True)
    asyncio.run(runtime.aclose())
    assert runtime.draining and not runtime.ready
    with pytest.raises(ValueError, match='store_closed'):
        runtime.sessions.open(opened.cookie_value, None)


def test_composition_instantiates_only_selected_adapter(tmp_path):
    context = pack(tmp_path)
    runtime = compose_runtime(context, {'configured': 'yes'}, callback_origin='https://agent.example:4200', required_kinds={'api'})
    async def scenario():
        await runtime.astart()
        assert runtime.pack_id == 'synthetic' and runtime.identity_adapter.version == 1
        assert runtime.sessions.pack_id == runtime.pack_id
        assert 'credential' not in repr(runtime)
        await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('source', [
    'raise RuntimeError("private-marker")',
    'def create(config): raise ValueError(config["private"])',
    'create = 42',
    ADAPTER.replace('def create(config):', 'async def create(config):'),
    ADAPTER.replace('async def login_hint', 'def login_hint'),
    ADAPTER.replace('version = 1', 'version = True'),
    ADAPTER.replace('version = 1', 'version = 2'),
    ADAPTER.replace('async def match_llm_identity(self, principal, llm_auth_context): return False', 'match_llm_identity = None'),
])
def test_factory_errors_are_redacted_and_do_not_leave_module_state(tmp_path, source):
    context = pack(tmp_path, source)
    before = set(sys.modules)
    with pytest.raises(IdentityExtensionError) as failure:
        load_identity_adapter(context, {'private': 'private-marker', 'configured': 'yes'})
    assert 'private-marker' not in str(failure.value)
    assert failure.value.__suppress_context__
    assert not (set(sys.modules) - before)


def test_loader_rejects_bad_values_disappeared_file_and_missing_spec(tmp_path):
    context = pack(tmp_path)
    with pytest.raises(IdentityExtensionError, match='resolved configuration'):
        load_identity_adapter(context, [])
    with patch('api_agent.auth.extensions.importlib.util.spec_from_file_location', return_value=None):
        with pytest.raises(IdentityExtensionError, match='could not be loaded'):
            load_identity_adapter(context, {})
    context.identity_factory_path.unlink()
    with pytest.raises(IdentityExtensionError, match='remain inside'):
        load_identity_adapter(context, {})


@pytest.mark.parametrize('identity', [
    'identity=1',
    '[authentication.identity]\nversion=true\npath="auth.py"\nfactory="create"',
    '[authentication.identity]\nversion=2\npath="auth.py"\nfactory="create"',
    '[authentication.identity]\nversion=1\npath="../auth.py"\nfactory="create"',
    '[authentication.identity]\nversion=1\npath="missing.py"\nfactory="create"',
    '[authentication.identity]\nversion=1\npath="auth.py"\nfactory="not.a.name"',
])
def test_declaration_rejects_invalid_factory_before_import(tmp_path, identity):
    context = pack(tmp_path, identity=False)
    (context.pack_root / 'pack.toml').write_text(
        'version=1\nid="pack"\nconfig="cfg.toml"\n' +
        ('[authentication]\n' if identity.startswith('identity=') else '') + identity)
    with pytest.raises(ResourcePackError, match='authentication.identity'):
        load_launch_context(context.base_root, context.pack_root)


def test_declaration_rejects_factory_symlink_outside_pack(tmp_path):
    context = pack(tmp_path)
    path = context.identity_factory_path
    path.unlink()
    outside = tmp_path / 'outside.py'
    outside.write_text(ADAPTER)
    path.symlink_to(outside)
    with pytest.raises(ResourcePackError, match='inside the resource pack'):
        load_launch_context(context.base_root, context.pack_root)


@pytest.mark.parametrize('production', ['"wrong"', '["../base"]', '["missing"]'])
def test_declaration_rejects_invalid_production_scope(tmp_path, production):
    with pytest.raises(ResourcePackError, match='production|Production'):
        pack(tmp_path, production=production)


def test_pack_production_scope_is_measured_without_a_src_directory(tmp_path):
    context = pack(tmp_path, production='[".", "."]')
    assert context.production_paths == (context.pack_root,)
    assert _production_sources(context, context.owner_root) == (context.pack_root,)
    assert _production_sources(context, context.base_root) == ()


@pytest.mark.parametrize('production', ['[]', '["other"]'])
def test_factory_cannot_be_omitted_from_production_coverage(tmp_path, production):
    context = pack(tmp_path, identity=False)
    (context.pack_root / 'other').mkdir()
    with pytest.raises(ResourcePackError, match='production coverage'):
        pack(tmp_path, production=production)


def test_factory_in_normal_src_does_not_need_extra_production_directory(tmp_path):
    context = pack(tmp_path, identity=False, production='[]')
    (context.pack_root / 'src').mkdir()
    context.pack_root.joinpath('auth.py').rename(context.pack_root / 'src/auth.py')
    with (context.pack_root / 'pack.toml').open('a') as stream:
        stream.write('[authentication.identity]\nversion=1\npath="src/auth.py"\nfactory="create"\n')
    context = load_launch_context(context.base_root, context.pack_root)
    assert context.production_paths == ()
    assert _production_sources(context, context.owner_root) == (context.owner_root / 'src',)


@pytest.mark.parametrize('key', ['', ' ', None, 1])
def test_principal_key_is_required(key):
    with pytest.raises(ValueError, match='Principal key'):
        VerifiedPrincipal(key)


def test_principal_and_public_errors_do_not_represent_credentials():
    principal = VerifiedPrincipal('opaque', credential='credential-marker')
    assert 'credential-marker' not in repr(principal)
    assert principal == VerifiedPrincipal('opaque', credential='different-marker')
    error = IdentityError('conflicting_credentials', status=400)
    assert str(error) == error.code == 'conflicting_credentials' and error.status == 400


@pytest.mark.parametrize('selected_pack', [False, True])
def test_pack_production_without_src_cannot_escape_actual_coverage_gate(tmp_path, selected_pack):
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.runner import run_unit_tests
    base = tmp_path / 'base'
    owner = tmp_path / 'pack'
    for root in (base, owner):
        (root / 'tests/unit').mkdir(parents=True)
        (root / 'tests/unit/test_ok.py').write_text('def test_ok():\n    assert 2 + 2 == 4\n')
    production = owner / 'integrations'
    production.mkdir()
    (production / 'never_imported.py').write_text('VALUE = 7\n')
    context = SimpleNamespace(base_root=base, owner_root=owner, production_paths=(production,),
                              suite_paths=lambda mode: [base / 'tests/unit', owner / 'tests/unit'])
    tests = collect_tests(context, 'unit')
    if not selected_pack:
        tests = [test for test in tests if test.repo == 'base']
    result = run_unit_tests(context, tests)
    assert all(test.status == 'passed' for test in result.tests)
    assert result.exit_code == 1
    assert any(area.repo == 'pack' and area.file.endswith('integrations/never_imported.py')
               and area.covered_lines == 0 and area.num_statements == 1
               for area in result.coverage)
