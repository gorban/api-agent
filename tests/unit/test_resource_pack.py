from pathlib import Path

import pytest

from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context


def make_pack(root, *, owner=".", unit=True):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text("version = 1\n")
    (root / "pack.toml").write_text(
        f'version = 1\nid = "fixture"\nowner = "{owner}"\nconfig = "config.toml"\n'
        '[defaults]\nUI_NX_APP = "shell"\n[tests]\n'
        + ('unit = ["tests/unit"]\n' if unit else '')
        + 'integration = ["tests/integration"]\ne2e = ["tests/e2e"]\n'
    )


def test_component_configuration_paths_are_ordered_and_owner_relative(tmp_path, monkeypatch):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    make_pack(base / 'demo')
    make_pack(pack)
    outside = tmp_path / 'adapter'
    outside.mkdir()
    (outside / 'config.toml').write_text('version=1\n')
    (pack / 'ui.toml').write_text('version=1\n')
    with (pack / 'pack.toml').open('a') as stream:
        stream.write('[providers]\nconfigs=["../adapter/config.toml","ui.toml","../adapter/config.toml"]\n')
    monkeypatch.chdir(base)
    context = load_launch_context(base, pack)
    assert context.provider_config_paths == (outside / 'config.toml', pack / 'ui.toml')


@pytest.mark.parametrize('entry', [
    'providers=7', 'providers={unknown=[]}', 'providers={configs="bad"}',
    'providers={configs=["missing.toml"]}', 'providers={configs=["config.txt"]}',
    'providers={configs=[""]}', 'providers={configs=[7]}',
])
def test_invalid_component_configuration_manifest_is_rejected(tmp_path, entry):
    base = tmp_path / 'base'
    make_pack(base / 'demo')
    (base / 'demo/config.txt').write_text('version=1\n')
    manifest = base / 'demo/pack.toml'
    manifest.write_text('version=1\nid="fixture"\nconfig="config.toml"\n' + entry + '\n')
    with pytest.raises(ResourcePackError, match='providers'):
        load_launch_context(base)


def test_demo_owner_and_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    base = tmp_path / "api-agent"
    make_pack(base / "demo", owner="..")
    monkeypatch.chdir(tmp_path)
    context = load_launch_context(base)
    assert context.pack_root == base / "demo"
    assert context.owner_root == base
    assert context.dotenv_path == base / ".env"
    assert context.tests["integration"] == (base / "tests/integration",)
    assert context.suite_paths("unit") == (base / "tests/unit",)


def test_external_pack_unit_suite_adds_base_and_absent_pack_units_are_allowed(tmp_path):
    base, pack = tmp_path / "base", tmp_path / "resource pack"
    base.mkdir()
    make_pack(pack)
    context = load_launch_context(base, pack)
    assert context.external_pack
    assert context.suite_paths("unit") == (base / "tests/unit", pack / "tests/unit")
    assert context.suite_paths("e2e") == (pack / "tests/e2e",)
    make_pack(pack, unit=False)
    assert load_launch_context(base, pack).suite_paths("unit") == (base / "tests/unit",)


def test_config_and_test_paths_have_explicit_anchors(tmp_path):
    base, pack = tmp_path / "base", tmp_path / "external"
    base.mkdir()
    make_pack(pack)
    context = load_launch_context(base, pack)
    assert context.config_path == pack / "config.toml"
    assert context.anchors == {"base_root": base, "pack_root": pack, "owner_root": pack}
    assert context.defaults["UI_NX_APP"] == "shell"


@pytest.mark.parametrize("replacement", [
    'version = 2\nid = "fixture"\nconfig = "config.toml"',
    'version = 1\nid = ""\nconfig = "config.toml"',
    'version = 1\nid = "fixture"\nconfig = "missing.toml"',
    'version = 1\nid = "fixture"\nconfig = "config.toml"\nunknown = true',
    'version = 1\nid = "fixture"\nconfig = "config.toml"\n[tests]\nunit = "tests/unit"',
])
def test_invalid_manifest_fails_before_dispatch(tmp_path, replacement):
    base, pack = tmp_path / "base", tmp_path / "pack"
    base.mkdir()
    make_pack(pack)
    (pack / "pack.toml").write_text(replacement)
    with pytest.raises(ResourcePackError):
        load_launch_context(base, pack)


def test_explicit_pack_wins_over_launcher_default(tmp_path):
    base, default, explicit = tmp_path / "base", tmp_path / "default", tmp_path / "explicit"
    base.mkdir()
    make_pack(default)
    make_pack(explicit)
    assert load_launch_context(base, explicit, default).pack_root == explicit


def test_external_pack_source_and_config_stay_separate_from_test_owner(tmp_path):
    base, pack = tmp_path / "base", tmp_path / "external"
    base.mkdir()
    make_pack(pack, owner="../base")
    context = load_launch_context(base, pack)
    assert context.owner_root == base
    assert context.external_pack
    assert context.dotenv_path == pack / ".env"
    assert context.suite_paths("unit") == (base / "tests/unit",)


def test_context_retains_launch_origin_with_external_pack(tmp_path):
    base, pack = tmp_path / "api-agent", tmp_path / "resource-pack"
    base.mkdir()
    make_pack(pack)
    assert load_launch_context(base, pack).launcher_root == base
    assert load_launch_context(base, pack, pack).launcher_root == pack


def test_bootstrap_hint_only_without_explicit_pack(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    demo = base / 'demo'
    demo.mkdir()
    (demo / 'pack.toml').write_text('version=1\nid="demo"\nconfig="config.toml"\n')
    (demo / 'config.toml').write_text('version=1\n')
    assert load_launch_context(base).show_pack_bootstrap
    assert load_launch_context(base, launcher_root=base).show_pack_bootstrap
    assert not load_launch_context(base, demo).show_pack_bootstrap
    assert not load_launch_context(base, launcher_root=demo).show_pack_bootstrap


def test_pack_declares_generic_credential_role_fields(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    make_pack(pack)
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text() + '\n[authentication]\ncredential_role_fields=["PARTNER_ROLE"]\n')
    assert load_launch_context(base, pack).credential_role_fields == ('PARTNER_ROLE',)


@pytest.mark.parametrize('declaration', [
    'authentication="invalid"',
    '[authentication]\nunknown=true',
    '[authentication]\ncredential_role_fields="PARTNER_ROLE"',
    '[authentication]\ncredential_role_fields=["bad-key"]',
    '[authentication]\ncredential_role_fields=[1]',
    '[authentication]\ncredential_role_fields=["ROLE","ROLE"]',
])
def test_invalid_credential_role_declarations_fail_before_dispatch(tmp_path, declaration):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    make_pack(pack)
    (pack / 'pack.toml').write_text('version=1\nid="fixture"\nconfig="config.toml"\n' + declaration + '\n')
    with pytest.raises(ResourcePackError, match='authentication'):
        load_launch_context(base, pack)


def test_manifest_service_identity_defaults_for_existing_packs(tmp_path):
    base,pack=tmp_path/'base',tmp_path/'external'
    base.mkdir()
    make_pack(pack)
    assert load_launch_context(base,pack).service_name=='api-agent'


@pytest.mark.parametrize('declaration', ['service_name=""','service_name=1','service_name="é-service"','service_name="bad\\nheader"'])
def test_manifest_invalid_service_identity_rejected(tmp_path,declaration):
    base,pack=tmp_path/'base',tmp_path/'external'
    base.mkdir()
    make_pack(pack)
    manifest=pack/'pack.toml'
    manifest.write_text(declaration+'\n'+manifest.read_text())
    with pytest.raises(ResourcePackError,match='service_name'):
        load_launch_context(base,pack)


def test_resource_pack_service_identity_is_cwd_independent(tmp_path, monkeypatch):
    base, pack = tmp_path / 'base', tmp_path / 'resource-pack'
    base.mkdir()
    make_pack(pack)
    manifest = pack / 'pack.toml'
    manifest.write_text('service_name="synthetic-api"\n' + manifest.read_text())
    monkeypatch.chdir(tmp_path)
    assert load_launch_context(base, pack).service_name == 'synthetic-api'


@pytest.mark.parametrize('configuration', ['', '[configuration]\n', '[configuration]\ndotenv=[]\n'])
def test_empty_shared_configuration_is_optional(tmp_path, configuration):
    base = tmp_path / 'base'
    make_pack(base / 'demo')
    with (base / 'demo/pack.toml').open('a') as stream:
        stream.write(configuration)
    assert load_launch_context(base).shared_dotenv_paths == ()


@pytest.mark.parametrize('entry', [
    'configuration=7', 'configuration={unknown=[]}',
    'configuration={dotenv="bad"}', 'configuration={dotenv=[7]}',
    'configuration={dotenv=[""]}', 'configuration={dotenv=["  "]}',
    'configuration={dotenv=["/absolute.env"]}',
])
def test_malformed_shared_configuration_is_rejected(tmp_path, entry):
    base = tmp_path / 'base'
    make_pack(base / 'demo')
    (base / 'demo/pack.toml').write_text(
        'version=1\nid="fixture"\nconfig="config.toml"\n' + entry + '\n')
    with pytest.raises(ResourcePackError, match='configuration'):
        load_launch_context(base)


def test_shared_paths_resolve_against_pack_and_deduplicate_missing_files(tmp_path, monkeypatch):
    base, pack = tmp_path / 'base', tmp_path / 'product'
    make_pack(base / 'demo')
    make_pack(pack)
    with (pack / 'pack.toml').open('a') as stream:
        stream.write('[configuration]\ndotenv=["../common/.env","optional.env","../common/.env"]\n')
    monkeypatch.chdir(base)
    context = load_launch_context(base, pack)
    assert context.shared_dotenv_paths == (tmp_path / 'common/.env', pack / 'optional.env')


def test_library_source_resolved_outside_sibling_roots_rejects(tmp_path, monkeypatch):
    from api_agent.runtime import resource_pack
    pack = tmp_path / 'pack'
    pack.mkdir()
    sibling = tmp_path / 'sibling'
    original = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda path, *args, **kwargs:
                        tmp_path.parent if path == sibling else original(path, *args, **kwargs))
    with pytest.raises(ResourcePackError, match='escapes sibling roots'):
        resource_pack._workflow_library(pack, {'source': '../sibling',
            'distribution': 'synthetic-workflow', 'resource': 'synthetic_workflows'})


def test_symlink_component_rejects_path_outside_declared_root(tmp_path):
    from api_agent.runtime.resource_pack import _has_symlink_component
    assert _has_symlink_component(tmp_path.parent / 'outside', tmp_path) is True


@pytest.mark.parametrize('error', [OSError, AttributeError, ValueError])
def test_distribution_identity_unreadable_provider_fails_closed(tmp_path, monkeypatch, error):
    from api_agent.runtime import resource_pack
    distribution = resource_pack.metadata.PathDistribution(tmp_path)
    original_stat = Path.stat
    def unavailable(path, *args, **kwargs):
        if path == tmp_path:
            raise error('unavailable synthetic provider')
        return original_stat(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'stat', unavailable)
        identity = resource_pack._distribution_identity(distribution)
    assert identity is None
