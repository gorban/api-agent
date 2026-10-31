"""Selected infrastructure declarations are inert, bounded captured data."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime.infrastructure_contract import (
    InfrastructureDeclarationError, load_infrastructure_contributions,
)
from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context


DECLARATION = '''version = 1
owner = "example"
[[services]]
id = "api"
compose = "services/api.json"
assets = ["assets/init.sql"]
dependencies = ["example/db"]
when = {field = "USE_LOCAL", equals = true}
readiness = {kind = "http", target = "api/ready", identity_field = "service", identity_value = "api"}
endpoints = [{name = "api", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}]
seed = {asset = "assets/init.sql", completion = {kind = "health"}}
'''


def _context(tmp_path, declaration=DECLARATION):
    pack = tmp_path / "pack"
    (pack / "services").mkdir(parents=True)
    (pack / "assets").mkdir()
    (pack / "infra.toml").write_text(declaration)
    (pack / "services/api.json").write_text('{"image":"example/api:1","environment":["READY=1"]}')
    (pack / "assets/init.sql").write_bytes(b"SELECT 1;\n")
    return SimpleNamespace(pack_root=pack, owner_root=pack, pack_id="example",
                           infrastructure_paths=(pack / "infra.toml",))


def test_captures_immutable_selected_service_without_execution(tmp_path):
    context = _context(tmp_path)

    (service,) = load_infrastructure_contributions(context)

    assert service.id == "example/api"
    assert service.activation.field == "USE_LOCAL"
    assert service.activation.equals is True
    assert service.dependencies == ("example/db",)
    assert service.compose["image"] == "example/api:1"
    assert service.compose["environment"] == ("READY=1",)
    assert service.assets[0].body == b"SELECT 1;\n"
    assert service.declaration_source.body == DECLARATION.encode()
    assert service.seed.completion.kind == "health"
    with pytest.raises(TypeError):
        service.compose["image"] = "changed"


@pytest.mark.parametrize("replacement", [
    'version = 2',
    'owner = "bad/name"',
    'id = "bad/name"',
    'compose = "../outside.json"',
    'when = {field = "USE_LOCAL", equals = []}',
    'dependencies = ["missing"]',
    'seed = {asset = "absent", completion = {kind = "health"}}',
])
def test_rejects_malformed_declaration_before_plan(tmp_path, replacement):
    original = {
        'version = 2': 'version = 1',
        'owner = "bad/name"': 'owner = "example"',
        'id = "bad/name"': 'id = "api"',
        'compose = "../outside.json"': 'compose = "services/api.json"',
        'when = {field = "USE_LOCAL", equals = []}': 'when = {field = "USE_LOCAL", equals = true}',
        'dependencies = ["missing"]': 'dependencies = ["example/db"]',
        'seed = {asset = "absent", completion = {kind = "health"}}':
            'seed = {asset = "assets/init.sql", completion = {kind = "health"}}',
    }[replacement]
    context = _context(tmp_path, DECLARATION.replace(original, replacement))

    with pytest.raises(InfrastructureDeclarationError):
        load_infrastructure_contributions(context)


def test_rejects_symlink_and_nonregular_assets(tmp_path):
    context = _context(tmp_path)
    asset = context.pack_root / "assets/init.sql"
    asset.unlink()
    asset.symlink_to(context.pack_root / "services/api.json")

    with pytest.raises(InfrastructureDeclarationError, match="capture"):
        load_infrastructure_contributions(context)


def test_rejects_fifo_and_oversized_asset_without_blocking(tmp_path):
    context = _context(tmp_path)
    asset = context.pack_root / "assets/init.sql"
    asset.unlink()
    import os
    os.mkfifo(asset)
    with pytest.raises(InfrastructureDeclarationError, match="regular"):
        load_infrastructure_contributions(context)
    asset.unlink()
    asset.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(InfrastructureDeclarationError, match="regular"):
        load_infrastructure_contributions(context)


@pytest.mark.parametrize(("old", "new"), [
    ('readiness = {kind = "http", target = "api/ready", identity_field = "service", identity_value = "api"}',
     'readiness = {kind = "tcp"}'),
    ('readiness = {kind = "http", target = "api/ready", identity_field = "service", identity_value = "api"}',
     'readiness = {kind = "http", target = "api/ready"}'),
    ('readiness = {kind = "http", target = "api/ready", identity_field = "service", identity_value = "api"}',
     'readiness = {kind = "health", target = "api/ready"}'),
    ('readiness = {kind = "http", target = "api/ready", identity_field = "service", identity_value = "api"}',
     'readiness = {kind = "health", timeout = 0}'),
    ('readiness = {kind = "http", target = "api/ready", identity_field = "service", identity_value = "api"}',
     'readiness = {kind = "health", extra = 1}'),
    ('id = "api"', 'id = "api"\nunknown = true'),
    ('when = {field = "USE_LOCAL", equals = true}', 'when = {field = "USE_LOCAL"}'),
    ('dependencies = ["example/db"]', 'dependencies = ["example/db", "example/db"]'),
    ('assets = ["assets/init.sql"]', 'assets = ["assets/init.sql", "assets/init.sql"]'),
    ('endpoints = [{name = "api", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}]',
     'endpoints = [{name = "api", host = 4, in_project = "http://api:8000"}]'),
    ('endpoints = [{name = "api", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}]',
     'endpoints = [{name = "api"}]'),
    ('endpoints = [{name = "api", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}]',
     'endpoints = [{name = "api", host = "a", in_project = "b"}, {name = "api", host = "c", in_project = "d"}]'),
    ('seed = {asset = "assets/init.sql", completion = {kind = "health"}}',
     'seed = {asset = "assets/init.sql"}'),
])
def test_rejects_invalid_service_fields(tmp_path, old, new):
    context = _context(tmp_path, DECLARATION.replace(old, new))

    with pytest.raises(InfrastructureDeclarationError):
        load_infrastructure_contributions(context)


def test_rejects_invalid_fragment_shape(tmp_path):
    context = _context(tmp_path)
    (context.pack_root / "services/api.json").write_text('[]')
    with pytest.raises(InfrastructureDeclarationError, match="service object"):
        load_infrastructure_contributions(context)


def test_rejects_symlinked_provider_declaration(tmp_path):
    context = _context(tmp_path)
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "factory.py").write_text("pass\n")
    (provider / "infrastructure.toml").symlink_to(context.pack_root / "infra.toml")

    with pytest.raises(InfrastructureDeclarationError, match="symlink"):
        load_infrastructure_contributions(context,
            selected_provider_locations=(str(provider / "factory.py") + ":factory",))


def test_rejects_invalid_toml_and_pack_owner_mismatch(tmp_path):
    context = _context(tmp_path)
    source = context.pack_root / "infra.toml"
    source.write_text("[")
    with pytest.raises(InfrastructureDeclarationError, match="TOML"):
        load_infrastructure_contributions(context)
    source.write_text(DECLARATION.replace('owner = "example"', 'owner = "other"'))
    with pytest.raises(InfrastructureDeclarationError, match="owner"):
        load_infrastructure_contributions(context)


def test_optional_activation_seed_and_provider_declaration(tmp_path):
    declaration = DECLARATION.replace('when = {field = "USE_LOCAL", equals = true}\n', '')
    declaration = declaration.replace('seed = {asset = "assets/init.sql", completion = {kind = "health"}}\n', '')
    context = _context(tmp_path, declaration)
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "factory.py").write_text("pass\n")

    (service,) = load_infrastructure_contributions(context,
        selected_provider_locations=(str(provider / "factory.py") + ":factory",))

    assert service.activation is None
    assert service.seed is None


@pytest.mark.parametrize(("old", "new"), [
    ('compose = "services/api.json"', 'compose = "/tmp/api.json"'),
    ('assets = ["assets/init.sql"]', 'assets = "assets/init.sql"'),
    ('endpoints = [{name = "api", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}]',
     'endpoints = "api"'),
])
def test_rejects_invalid_path_or_collection(tmp_path, old, new):
    context = _context(tmp_path, DECLARATION.replace(old, new))
    with pytest.raises(InfrastructureDeclarationError):
        load_infrastructure_contributions(context)


def test_detects_asset_change_during_capture(tmp_path, monkeypatch):
    context = _context(tmp_path)
    import api_agent.runtime.infrastructure_contract as contract
    real_fstat = contract.os.fstat
    calls = 0

    def changed_after_read(fd):
        nonlocal calls
        calls += 1
        info = real_fstat(fd)
        if calls == 2:
            class Changed:
                st_dev, st_ino, st_size = info.st_dev, info.st_ino, info.st_size
                st_mtime_ns = info.st_mtime_ns + 1
            return Changed()
        return info

    monkeypatch.setattr(contract, 'os', SimpleNamespace(**(vars(contract.os) | {'fstat': changed_after_read})))
    with pytest.raises(InfrastructureDeclarationError, match="changed"):
        load_infrastructure_contributions(context)


def test_rejects_declaration_and_service_count_limits(tmp_path):
    context = _context(tmp_path)
    context.infrastructure_paths = (context.pack_root / "infra.toml",) * 17
    with pytest.raises(InfrastructureDeclarationError, match="too many"):
        load_infrastructure_contributions(context)
    context.infrastructure_paths = (context.pack_root / "infra.toml",)
    source = context.pack_root / "infra.toml"
    prefix = 'version = 1\nowner = "example"\n'
    service = ('[[services]]\nid = "api{n}"\ncompose = "services/api.json"\n'
               'readiness = {{kind = "health"}}\n')
    source.write_text(prefix + ''.join(service.format(n=n) for n in range(33)))
    with pytest.raises(InfrastructureDeclarationError, match="budget"):
        load_infrastructure_contributions(context)


def test_rejects_too_many_endpoint_outputs(tmp_path):
    entries = ', '.join(
        f'{{name = "api{n}", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}}'
        for n in range(17))
    original = 'endpoints = [{name = "api", host = "http://127.0.0.1:8000", in_project = "http://api:8000"}]'
    context = _context(tmp_path, DECLARATION.replace(original, f'endpoints = [{entries}]'))
    with pytest.raises(InfrastructureDeclarationError, match="endpoint"):
        load_infrastructure_contributions(context)


def test_relocated_pack_capture_is_cwd_independent_and_missing_asset_rejects(tmp_path, monkeypatch):
    import shutil

    context = _context(tmp_path)
    relocated = tmp_path / "installed" / "pack"
    shutil.copytree(context.pack_root, relocated)
    context.pack_root = relocated
    context.owner_root = relocated
    context.infrastructure_paths = (relocated / "infra.toml",)
    shutil.rmtree(tmp_path / "pack")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    (service,) = load_infrastructure_contributions(context)
    assert service.compose_source.body == (relocated / "services/api.json").read_bytes()
    assert service.assets[0].body == b"SELECT 1;\n"

    (relocated / "assets/init.sql").unlink()
    with pytest.raises(InfrastructureDeclarationError, match="capture"):
        load_infrastructure_contributions(context)


def test_rejects_duplicate_json_keys_and_nonfinite_constant(tmp_path):
    context = _context(tmp_path)
    fragment = context.pack_root / "services/api.json"
    fragment.write_text('{"image":"a:1","image":"b:1"}')
    with pytest.raises(InfrastructureDeclarationError, match="Compose JSON"):
        load_infrastructure_contributions(context)
    fragment.write_text('{"image":NaN}')
    with pytest.raises(InfrastructureDeclarationError, match="Compose JSON"):
        load_infrastructure_contributions(context)


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_rejects_overflowing_json_numbers(tmp_path, number):
    context = _context(tmp_path)
    (context.pack_root / "services/api.json").write_text('{"image":"example:1","x":' + number + '}')

    with pytest.raises(InfrastructureDeclarationError, match="Compose JSON"):
        load_infrastructure_contributions(context)


def test_finite_json_fraction_is_captured(tmp_path):
    context = _context(tmp_path)
    (context.pack_root / "services/api.json").write_text('{"image":"example:1","weight":0.25}')

    (service,) = load_infrastructure_contributions(context)

    assert service.compose["weight"] == 0.25


@pytest.mark.parametrize("field", [
    'target = ""', 'status = 200', 'identity_field = ""', 'identity_value = ""',
])
def test_health_rejects_presence_of_http_only_field(tmp_path, field):
    declaration = DECLARATION.replace(
        'completion = {kind = "health"}', 'completion = {kind = "health", ' + field + '}')
    context = _context(tmp_path, declaration)

    with pytest.raises(InfrastructureDeclarationError, match="health readiness"):
        load_infrastructure_contributions(context)


def test_ancestor_symlink_cannot_redirect_captured_asset(tmp_path):
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "init.sql").write_text("SELECT 9;\n")
    import shutil
    shutil.rmtree(context.pack_root / "assets")
    (context.pack_root / "assets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InfrastructureDeclarationError, match="capture"):
        load_infrastructure_contributions(context)


@pytest.mark.parametrize("version", ["true", "1.0"])
def test_declaration_version_rejects_noninteger_equivalent(tmp_path, version):
    context = _context(tmp_path, DECLARATION.replace("version = 1", "version = " + version, 1))

    with pytest.raises(InfrastructureDeclarationError, match="unsupported"):
        load_infrastructure_contributions(context)


def test_nested_compose_object_is_immutable(tmp_path):
    context = _context(tmp_path)
    (context.pack_root / "services/api.json").write_text(
        '{"image":"example:1","labels":{"outer":{"inner":"value"}}}')

    (service,) = load_infrastructure_contributions(context)

    with pytest.raises(TypeError):
        service.compose["labels"]["outer"]["inner"] = "changed"


def test_selected_provider_metadata_only_without_factory_import(tmp_path):
    context = _context(tmp_path)
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "factory.py").write_text("raise AssertionError('adapter imported')\n")
    (provider / "infrastructure.toml").write_text(DECLARATION.replace('owner = "example"', 'owner = "provider"'))
    (provider / "services").mkdir()
    (provider / "services/api.json").write_text('{"image":"example/provider:1"}')
    (provider / "assets").mkdir()
    (provider / "assets/init.sql").write_text("SELECT 2;\n")

    services = load_infrastructure_contributions(context,
        selected_provider_locations=(str(provider / "factory.py") + ":factory",))

    assert {service.id for service in services} == {"example/api", "provider/api"}


def test_duplicate_selected_provider_service_rejects(tmp_path):
    context = _context(tmp_path)
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "factory.py").write_text("raise AssertionError('adapter imported')\n")
    (provider / "infrastructure.toml").write_text(DECLARATION)
    (provider / "services").mkdir()
    (provider / "services/api.json").write_text('{}')
    (provider / "assets").mkdir()
    (provider / "assets/init.sql").write_text("SELECT 2;\n")

    with pytest.raises(InfrastructureDeclarationError, match="duplicate"):
        load_infrastructure_contributions(context,
            selected_provider_locations=(str(provider / "factory.py") + ":factory",))


def test_pack_manifest_declares_paths_without_loading_services(tmp_path):
    base = tmp_path / "base"
    pack = tmp_path / "pack"
    base.mkdir()
    pack.mkdir()
    (pack / "cfg.toml").write_text("version = 1\n[fields]\n")
    (pack / "infra.toml").write_text("version = 1\nowner = 'example'\nservices = []\n")
    (pack / "pack.toml").write_text('version=1\nid="example"\nconfig="cfg.toml"\n'
        '[infrastructure]\nversion=1\ndeclarations=["infra.toml"]\n')

    context = load_launch_context(base, resource_pack_dir=pack)

    assert context.infrastructure_paths == (pack / "infra.toml",)


def test_pack_manifest_rejects_unsupported_infrastructure_version(tmp_path):
    base = tmp_path / "base"
    pack = tmp_path / "pack"
    base.mkdir()
    pack.mkdir()
    (pack / "cfg.toml").write_text("version=1\n[fields]\n")
    (pack / "pack.toml").write_text('version=1\nid="example"\nconfig="cfg.toml"\n'
        '[infrastructure]\nversion=2\ndeclarations=[]\n')

    with pytest.raises(ResourcePackError, match="infrastructure"):
        load_launch_context(base, resource_pack_dir=pack)


@pytest.mark.parametrize("section", [
    '[infrastructure]\nversion=1\n',
    '[infrastructure]\nversion=1\ndeclarations="infra.toml"\n',
    '[infrastructure]\nversion=1\ndeclarations=[]\nextra=true\n',
    '[infrastructure]\nversion=1\ndeclarations=["/tmp/infra.toml"]\n',
    '[infrastructure]\nversion=1\ndeclarations=["../infra.toml"]\n',
    '[infrastructure]\nversion=1\ndeclarations=["infra.json"]\n',
    '[infrastructure]\nversion=1\ndeclarations=["infra.toml", "infra.toml"]\n',
    '[infrastructure]\nversion=1\ndeclarations=["missing.toml"]\n',
    '[infrastructure]\nversion=1\ndeclarations=[' + ','.join('"infra.toml"' for _ in range(17)) + ']\n',
])
def test_pack_manifest_rejects_invalid_infrastructure_metadata(tmp_path, section):
    base = tmp_path / "base"
    pack = tmp_path / "pack"
    base.mkdir()
    pack.mkdir()
    (pack / "cfg.toml").write_text("version=1\n[fields]\n")
    (pack / "infra.toml").write_text("version=1\nowner='example'\nservices=[]\n")
    (pack / "pack.toml").write_text('version=1\nid="example"\nconfig="cfg.toml"\n' + section)
    with pytest.raises(ResourcePackError, match="infrastructure"):
        load_launch_context(base, resource_pack_dir=pack)


def test_pack_manifest_rejects_symlinked_infrastructure_file(tmp_path):
    base = tmp_path / "base"
    pack = tmp_path / "pack"
    base.mkdir()
    pack.mkdir()
    (pack / "cfg.toml").write_text("version=1\n[fields]\n")
    target = tmp_path / "outside.toml"
    target.write_text("version=1\n")
    (pack / "infra.toml").symlink_to(target)
    (pack / "pack.toml").write_text('version=1\nid="example"\nconfig="cfg.toml"\n'
        '[infrastructure]\nversion=1\ndeclarations=["infra.toml"]\n')
    with pytest.raises(ResourcePackError, match="infrastructure"):
        load_launch_context(base, resource_pack_dir=pack)
