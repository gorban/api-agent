from types import SimpleNamespace

import pytest


def seed_renderer_declaration(root, default=''):
    import json
    (root / 'cfg').mkdir(parents=True, exist_ok=True)
    (root / 'cfg/config.toml').write_text('version=1\n[fields.API_AGENT_PLANTUML_JAR]\n'
        'type="path"\nanchor="owner_root"\nallow_empty=true\ndefault=' + json.dumps(default) + '\n')


@pytest.mark.parametrize(('argv', 'environ', 'expected'), [
    ([], {}, 'dotenv.jar'),
    ([], {'API_AGENT_PLANTUML_JAR': 'environment.jar'}, 'environment.jar'),
    (['--api-agent-plantuml-jar', 'command.jar'], {'API_AGENT_PLANTUML_JAR': 'environment.jar'}, 'command.jar'),
])
def test_renderer_configuration_uses_normal_precedence(tmp_path, argv, environ, expected):
    from api_agent.workflows.viewer_config import resolve_renderer_path
    seed_renderer_declaration(tmp_path)
    (tmp_path / '.env').write_text('API_AGENT_PLANTUML_JAR=dotenv.jar\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, launcher_root=None,
                              shared_dotenv_paths=(), dotenv_path=tmp_path / '.env',
                              anchors={'owner_root': tmp_path})
    assert resolve_renderer_path(context, argv=argv, environ=environ) == str(tmp_path / expected)


def test_default_renderer_accepts_explicit_resolved_path(tmp_path, monkeypatch):
    from api_agent.workflows.viewer_render import default_renderer
    jar = tmp_path / 'resolved.jar'
    jar.touch()
    monkeypatch.setenv('API_AGENT_PLANTUML_JAR', str(tmp_path / 'ignored.jar'))
    assert default_renderer(str(jar)).jar == jar


def test_renderer_declaration_retains_pack_conditional_controllers(tmp_path, monkeypatch):
    from api_agent.config import compose_schema, schema_from_dict
    from api_agent.workflows.viewer_config import resolve_renderer_path
    from api_agent.runtime import pack_composition
    seed_renderer_declaration(tmp_path)
    def composed(context, shared):
        extension = schema_from_dict({'version': 1, 'fields': {
            'SELECT_RENDERER': {'type': 'bool', 'default': True, 'pack_only': True},
            'API_AGENT_PLANTUML_JAR': {'type': 'path', 'anchor': 'owner_root', 'override': True,
                'conditional_defaults': [{'when': {'SELECT_RENDERER': True}, 'value': 'selected.jar'}]},
        }}, base_schema=shared)
        return compose_schema(shared, extension)
    monkeypatch.setattr(pack_composition, 'load_context_schema', composed)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, launcher_root=None,
        shared_dotenv_paths=(), dotenv_path=tmp_path / '.env', anchors={'owner_root': tmp_path},
        pack_layers=(), external_pack=True)
    assert resolve_renderer_path(context, environ={}) == str(tmp_path / 'selected.jar')


def test_generation_renderer_configuration_uses_explicit_owner_root(tmp_path, monkeypatch):
    from api_agent.workflows.viewer_config import resolve_renderer_path
    owner = tmp_path / 'owner'
    owner.mkdir()
    (owner / '.env').write_text('API_AGENT_PLANTUML_JAR=owner.jar\n')
    monkeypatch.chdir(tmp_path)
    assert resolve_renderer_path(root=owner, environ={}) == str(owner / 'owner.jar')


def test_generation_cli_threads_resolved_renderer(monkeypatch, tmp_path):
    from api_agent.workflows import viewer_build
    jar = tmp_path / 'selected.jar'
    jar.touch()
    calls = []
    monkeypatch.setattr(viewer_build, 'generate_views', lambda root, **kwargs: calls.append(kwargs) or ())
    assert viewer_build.main(['generate', '--root', str(tmp_path),
                              '--api-agent-plantuml-jar', 'selected.jar']) == 0
    assert calls[0]['renderer'].jar == jar


def test_bootstrap_pack_selector_is_documented_as_pre_schema_input():
    from api_agent.config import load_shared_schema
    field = load_shared_schema().fields['RESOURCE_PACK_DIR']
    assert field.type == 'str'
    assert not field.acknowledge_default
    assert 'dotenv does not select packs' in field.help


def test_renderer_defaults_belong_to_selected_base(tmp_path):
    from api_agent.workflows.viewer_config import resolve_renderer_path
    seed_renderer_declaration(tmp_path, 'selected-base.jar')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, launcher_root=None,
        shared_dotenv_paths=(), dotenv_path=tmp_path / '.env', anchors={'owner_root': tmp_path})
    assert resolve_renderer_path(context, environ={}) == str(tmp_path / 'selected-base.jar')
