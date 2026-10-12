"""Distribution declarations are read without importing provider code."""
from types import SimpleNamespace
import pytest

from api_agent.providers.distribution import read_provider_manifest, selected_manifests
from api_agent.runtime.dependencies import requirements


def test_active_provider_manifest_adds_only_selected_requirements(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    active = tmp_path / 'active'
    active.mkdir()
    inactive = tmp_path / 'inactive'
    inactive.mkdir()
    (active / 'provider-dependencies.toml').write_text(
        'version = 1\ndependencies = ["synthetic-client>=1", "synthetic-client>=1"]\n'
        '[groups]\nvisual = ["synthetic-ui>=2"]\n')
    (inactive / 'provider-dependencies.toml').write_text(
        'version = 1\ndependencies = ["unrelated-client>=9"]\n')
    context = SimpleNamespace(base_root=base, pack_root=base)
    selected = read_provider_manifest(active, owner='synthetic')
    assert selected.requirements(groups=('visual',)) == ('synthetic-client>=1', 'synthetic-ui>=2')
    assert selected.select_groups('visual').requirements() == ('synthetic-client>=1', 'synthetic-ui>=2')
    assert requirements(context) == ()
    assert requirements(context, selected_provider_manifests=(selected,)) == ('synthetic-client>=1',)
    assert requirements(context, selected_provider_manifests=(selected.select_groups('visual'),)) == (
        'synthetic-client>=1', 'synthetic-ui>=2')
    with pytest.raises(TypeError, match='manifest descriptors'):
        requirements(context, selected_provider_manifests=('factory.py:factory',))
    (active / 'factory.py').write_text('raise RuntimeError("must remain inert")\n')
    values = {'LLM_PROVIDER': str(active / 'factory.py') + ':factory',
              'UI_PROVIDER': str(inactive / 'missing.py') + ':factory'}
    active_descriptors = selected_manifests(values, owner_root=base, active_kinds={'llm'})
    assert len(active_descriptors) == 1
    assert active_descriptors[0].root == active
    assert active_descriptors[0].requirements() == selected.requirements()
    values['API_PROVIDER'] = values['LLM_PROVIDER']
    assert len(selected_manifests(values, owner_root=base, active_kinds={'llm', 'api'})) == 1
    combined = selected_manifests(values, owner_root=base, active_kinds={'llm', 'api'},
                                  groups_by_kind={'api': ('visual',)})
    assert combined[0].requirements() == ('synthetic-client>=1', 'synthetic-ui>=2')
    assert selected_manifests(values, owner_root=base, active_kinds=set()) == ()
    with pytest.raises(ValueError, match='Unknown provider kind'):
        selected_manifests(values, owner_root=base, active_kinds={'unsupported'})


def test_provider_manifest_is_strict_and_inert(tmp_path):
    manifest = tmp_path / 'provider-dependencies.toml'
    manifest.write_text('version = 1\ndependencies = ["synthetic-client>=1"]\n[groups]\nvisual = ["synthetic-ui>=2"]\n')
    selected = read_provider_manifest(tmp_path, owner='sample')
    with pytest.raises(ValueError, match='Unknown provider dependency group'):
        selected.requirements(groups=('missing',))
    manifest.write_text('version = 1\ndependencies = [42]\n')
    with pytest.raises(ValueError, match='nonempty requirement strings'):
        read_provider_manifest(tmp_path, owner='sample')
    manifest.write_text('version = 1\nunknown = true\n')
    with pytest.raises(ValueError, match='requires version'):
        read_provider_manifest(tmp_path, owner='sample')


def test_missing_provider_manifest_has_no_dependencies(tmp_path):
    assert read_provider_manifest(tmp_path, owner='sample').requirements() == ()


def test_manifest_root_and_symlink_escape_are_rejected(tmp_path):
    with pytest.raises(ValueError, match='existing root'):
        read_provider_manifest(tmp_path / 'absent', owner='sample')
    with pytest.raises(ValueError, match='owner'):
        read_provider_manifest(tmp_path, owner='')
    outside = tmp_path.parent / 'outside-provider-dependencies.toml'
    outside.write_text('version = 1\n')
    (tmp_path / 'provider-dependencies.toml').symlink_to(outside)
    with pytest.raises(ValueError, match='inside its root'):
        read_provider_manifest(tmp_path, owner='sample')


def test_fifo_manifest_is_rejected_without_reading_it(tmp_path):
    import os
    os.mkfifo(tmp_path / 'provider-dependencies.toml')
    with pytest.raises(ValueError, match='regular file'):
        read_provider_manifest(tmp_path, owner='sample')


def test_manifest_rechecks_open_descriptor_and_reports_open_failure(tmp_path, monkeypatch):
    import os
    import stat
    from types import SimpleNamespace
    manifest = tmp_path / 'provider-dependencies.toml'
    manifest.write_text('version = 1\n')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fstat', lambda descriptor: SimpleNamespace(st_mode=stat.S_IFIFO))
        with pytest.raises(ValueError, match='regular file'):
            read_provider_manifest(tmp_path, owner='sample')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('denied')))
        with pytest.raises(ValueError, match='cannot be read safely'):
            read_provider_manifest(tmp_path, owner='sample')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fstat', lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG, st_dev=999, st_ino=999))
        with pytest.raises(ValueError, match='regular file'):
            read_provider_manifest(tmp_path, owner='sample')


def test_manifest_open_without_optional_flags_uses_file_identity(tmp_path, monkeypatch):
    import os
    (tmp_path / 'provider-dependencies.toml').write_text('version = 1\n')
    with monkeypatch.context() as patch:
        patch.delattr(os, 'O_NOFOLLOW', raising=False)
        patch.delattr(os, 'O_NONBLOCK', raising=False)
        assert read_provider_manifest(tmp_path, owner='sample').requirements() == ()


def test_complete_relocated_pack_reads_manifests_and_schemas_without_factory_import(tmp_path):
    from pathlib import Path
    import shutil
    from api_agent.providers.distribution import inspect_relocated_pack

    base = Path(__file__).resolve().parents[2]
    original = tmp_path / 'source-pack'
    original.mkdir()
    (original / 'cfg').mkdir()
    (original / 'providers').mkdir()
    (original / 'src').mkdir()
    (original / 'pack.toml').write_text(
        'version = 1\nid = "synthetic"\nowner = "."\nconfig = "cfg/config.toml"\n'
        'production = ["src"]\n[providers]\nconfigs = ["providers/config.toml"]\n'
        '[defaults]\nUI_NX_APP = "synthetic-ui"\n')
    (original / 'cfg/config.toml').write_text(
        'version = 1\n[fields.SYNTHETIC_FLAG]\ntype = "bool"\ndefault = true\n'
        '[fields.LLM_PROVIDER]\ntype = "str"\noverride = true\n'
        'default = "src/factory.py:factory"\n')
    (original / 'providers/config.toml').write_text(
        'version = 1\n[fields.SYNTHETIC_MODE]\ntype = "str"\ndefault = "enabled"\n')
    (original / 'src/provider-dependencies.toml').write_text(
        'version = 1\ndependencies = ["synthetic-client>=1"]\n')
    (original / 'src/factory.py').write_text('raise RuntimeError("must remain inert")\n')
    (original / 'src/cfg').mkdir()
    (original / 'src/cfg/config.toml').write_text(
        'version = 1\n[fields.FACTORY_FLAG]\ntype = "bool"\ndefault = true\n')
    installed = tmp_path / 'installed' / 'pack'
    shutil.copytree(original, installed)

    context, schema = inspect_relocated_pack(base, installed)
    selected = selected_manifests({'LLM_PROVIDER': str(installed / 'src/factory.py') + ':factory'},
                                  owner_root=installed, active_kinds={'llm'})
    assert context.pack_root == installed
    assert context.production_paths == (installed / 'src',)
    assert schema.fields['SYNTHETIC_FLAG'].default is True
    assert schema.fields['SYNTHETIC_MODE'].default == 'enabled'
    assert schema.fields['FACTORY_FLAG'].default is True
    assert schema.fields['UI_NX_APP'].default == 'synthetic-ui'
    assert len(selected) == 1
    assert selected[0].root == installed / 'src'
    assert selected[0].requirements() == ('synthetic-client>=1',)
    (installed / 'src/cfg/config.toml').write_text(
        'version = 1\n[fields.FACTORY_FLAG]\ntype = "invalid"\n')
    with pytest.raises(ValueError, match='invalid type'):
        inspect_relocated_pack(base, installed)
    (installed / 'src/cfg/config.toml').write_text('version = 1\n')
    (installed / 'src/provider-dependencies.toml').write_text('version = 1\ndependencies = [42]\n')
    with pytest.raises(ValueError, match='nonempty requirement strings'):
        inspect_relocated_pack(base, installed)
    (installed / 'src/provider-dependencies.toml').write_text(
        'version = 1\ndependencies = ["synthetic-client>=1"]\n')
    (installed / 'src/factory.py').unlink()
    with pytest.raises(ValueError, match='provider_location_invalid'):
        inspect_relocated_pack(base, installed)


@pytest.mark.parametrize('hostile', [
    'demo@https://example.invalid/x.whl',
    'git+https://example.invalid/repository',
    '--extra-index-url=https://example.invalid/simple',
    'demo @ file:///tmp/demo.whl',
])
@pytest.mark.parametrize('source', ['base', 'pack', 'provider'])
def test_hostile_requirement_never_reaches_environment_creation(tmp_path, monkeypatch, hostile, source):
    from unittest.mock import Mock
    from api_agent.runtime.dependencies import ensure_environment

    base, pack, provider = (tmp_path / name for name in ('base', 'pack', 'provider'))
    for root in (base, pack, provider):
        root.mkdir()
    if source == 'provider':
        (provider / 'provider-dependencies.toml').write_text(
            f'version = 1\ndependencies = [{hostile!r}]\n'.replace("'", '"'))
        selected = (read_provider_manifest(provider, owner='sample'),)
    else:
        target = base if source == 'base' else pack
        (target / 'pyproject.toml').write_text(
            f'[project]\ndependencies = [{hostile!r}]\n'.replace("'", '"'))
        selected = ()
    context = SimpleNamespace(base_root=base, pack_root=pack, external_pack=True)
    installer = Mock()
    monkeypatch.setattr('api_agent.runtime.dependencies.subprocess.run', installer)
    with pytest.raises(ValueError, match='Direct URL|PEP 508'):
        ensure_environment(context, [], selected_provider_manifests=selected)
    installer.assert_not_called()
    assert not (pack / '.venv').exists()


def test_requirement_parser_uses_pip_vendor_when_top_level_packaging_is_unavailable(monkeypatch):
    import builtins
    from api_agent.runtime.dependencies import _parsed_requirement

    original_import = builtins.__import__
    def missing_top_level(name, *args, **kwargs):
        if name == 'packaging.requirements':
            raise ImportError('not installed')
        if name == 'pip._vendor.packaging.requirements':
            return original_import('packaging.requirements', *args, **kwargs)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing_top_level)
    assert _parsed_requirement('synthetic-client>=1').name == 'synthetic-client'


def test_requirement_parser_fails_closed_without_parser_or_string(monkeypatch):
    import builtins
    from api_agent.runtime.dependencies import _parsed_requirement

    with pytest.raises(ValueError, match='PEP 508 string'):
        _parsed_requirement(42)
    original_import = builtins.__import__
    def missing_parsers(name, *args, **kwargs):
        if name in {'packaging.requirements', 'pip._vendor.packaging.requirements'}:
            raise ImportError('not installed')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing_parsers)
    with pytest.raises(OSError, match='needs packaging or pip'):
        _parsed_requirement('synthetic-client>=1')


def test_failed_target_probe_does_not_start_installer(tmp_path, monkeypatch):
    import subprocess
    from unittest.mock import Mock
    from api_agent.runtime.dependencies import ensure_environment

    (tmp_path / 'pyproject.toml').write_text('[project]\ndependencies = ["synthetic-client>=1"]\n')
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    probe = Mock(side_effect=subprocess.CalledProcessError(1, 'probe'))
    monkeypatch.setattr('api_agent.runtime.dependencies.subprocess.run', probe)
    with pytest.raises(OSError, match='Cannot verify dependencies'):
        ensure_environment(context, [])
    assert probe.call_count == 1
    assert not (tmp_path / '.venv/.api-agent-dependencies').exists()
