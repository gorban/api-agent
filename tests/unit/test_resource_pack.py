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
