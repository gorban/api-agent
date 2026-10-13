"""Optional shared files participate in ordinary provider discovery."""
from pathlib import Path
from unittest.mock import patch

from api_agent.config import compose_schema, load_declarations, load_shared_schema
from api_agent.config.session import ConfigSession
from api_agent.runtime.bootstrap import generated_help, main
from api_agent.runtime.resource_pack import load_launch_context


def test_shared_provider_selection_reaches_bootstrap_and_session(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    base = tmp_path / "base"
    (base / "cfg").mkdir(parents=True)
    (base / "cfg/config.toml").write_bytes((checkout / "cfg/config.toml").read_bytes())
    common, product = tmp_path / 'common', tmp_path / 'product'
    common.mkdir()
    product.mkdir()
    adapter = common / 'adapter'
    (adapter / 'cfg').mkdir(parents=True)
    (adapter / 'provider.py').write_text("raise AssertionError('provider factory executed')\n")
    (adapter / 'cfg/config.toml').write_text(
        'version=1\n[fields.COMMON_VALUE]\ntype="str"\ndefault="synthetic-default"\n')
    (common / '.env').write_text('UI_PROVIDER=' + str(adapter / 'provider.py') + ':create\nCOMMON_VALUE=synthetic-saved\n')
    (product / 'config.toml').write_text('version=1\n')
    (product / 'pack.toml').write_text(
        'version=1\nid="product"\nconfig="config.toml"\n'
        '[configuration]\ndotenv=["../common/.env","optional.env"]\n')
    original = (common / '.env').read_bytes()
    (common / '.env').chmod(0o444)
    context = load_launch_context(base, product)
    shared = load_shared_schema(base)
    schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared, source='pack_default'))
    with patch.dict('os.environ', {}, clear=True), patch(
            'api_agent.runtime.bootstrap.generated_help', wraps=generated_help) as help_output:
        assert main(['--help', '--api-agent-dir=' + str(base)], base_root=base, resource_pack_dir=product, _worktrees_verified=True) == 0
    assert 'COMMON_VALUE' in help_output.call_args.args[0].fields
    session = ConfigSession(context, schema, environ={})
    assert session.result().values['COMMON_VALUE'] == 'synthetic-saved'
    assert session.result().fields['COMMON_VALUE'].source == session._source(common / '.env')
    assert 'COMMON_VALUE' in session.schema.fields
    assert any(row.name == 'COMMON_VALUE' and row.present for row in session.cleanup_rows())
    assert session.result(_review=True).values['COMMON_VALUE'] == 'synthetic-saved'
    assert (common / '.env').read_bytes() == original


def test_bootstrap_orders_and_deduplicates_shared_files(tmp_path):
    from api_agent.config import read_dotenv, resolve_config
    checkout = Path(__file__).resolve().parents[2]
    base, common, launcher, product = (tmp_path / name for name in ('base', 'common', 'launcher', 'product'))
    for folder in (base, common, launcher, product):
        folder.mkdir()
    (base / 'cfg').mkdir()
    (base / 'cfg/config.toml').write_bytes((checkout / 'cfg/config.toml').read_bytes())
    (product / 'config.toml').write_text('version=1\n[fields.VALUE]\ntype="int"\ndefault=1\n')
    (product / 'pack.toml').write_text(
        'version=1\nid="product"\nconfig="config.toml"\n'
        '[configuration]\ndotenv=["../common/.env","../base/.env","../common/.env"]\n')
    paths = tuple(folder / '.env' for folder in (base, common, launcher, product))
    for value, path in enumerate(paths, 2):
        path.write_text(f'VALUE={value}\n')
    with patch.dict('os.environ', {}, clear=True), patch(
            'api_agent.config.read_dotenv', wraps=read_dotenv) as reader, patch(
            'api_agent.config.resolve_config', wraps=resolve_config) as resolver:
        assert main(['--help', '--api-agent-dir=' + str(base)], base_root=base, resource_pack_dir=product,
                    launcher_root=launcher, _worktrees_verified=True) == 0
    assert tuple(call.args[0] for call in reader.call_args_list) == paths
    assert resolver.call_args.kwargs['dotenv']['VALUE'] == '5'
