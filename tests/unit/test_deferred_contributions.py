"""Deferred declarations retain sibling context and ordered source authority."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os

import pytest

from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config
from api_agent.providers.loading import bootstrap_schema
from api_agent.runtime.bootstrap import main
from api_agent.runtime.resource_pack import load_launch_context


PREDICATES = [
    'applies_when = {{ MODE = {value} }}',
    'required_when = {{ MODE = {value} }}',
    'conditional_defaults = [{{ when = {{ MODE = {value} }}, value = "plain" }}]',
]


def deferred_pack(tmp_path, predicate, value, controller_first):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    (base / 'cfg').mkdir(parents=True)
    (base / 'cfg/config.toml').write_bytes((Path(__file__).parents[2] / 'cfg/config.toml').read_bytes())
    adapter = pack / 'adapter'
    (adapter / 'cfg').mkdir(parents=True)
    (adapter / 'provider.py').write_text("raise AssertionError('inert discovery must not import a factory')\n")
    (adapter / 'cfg/config.toml').write_text('version=1\n[fields.VALUE]\ntype="str"\n'
        '[fields.MODE]\ntype="int"\nvalidation="nonnegative"\ndefault=0\n')
    (pack / 'config.toml').write_text('version=1\n[fields.LLM_PROVIDER]\ntype="str"\n'
        'override=true\ndefault="adapter/provider.py:factory"\n')
    (pack / 'pack.toml').write_text('version=1\nid="fixture"\nconfig="config.toml"\n'
        '[providers]\nconfigs=["late.toml"]\n')
    controlled = '[fields.VALUE]\ntype="str"\noverride=true\noverride_if_present=true\n'
    controlled += 'default="plain"\n' + predicate.format(value=value) + '\n'
    controller = '[fields.MODE]\ntype="int"\noverride=true\noverride_if_present=true\n'
    controller += 'validation="positive"\ndefault=1\n'
    late = pack / 'late.toml'
    late.write_text('version=1\n' + (controller + controlled if controller_first else controlled + controller))
    context = load_launch_context(base, pack)
    shared = load_shared_schema(base)
    initial = compose_schema(shared, load_declarations(context.config_path, base_schema=shared))
    return SimpleNamespace(base=base, pack=pack, late=late, context=context, initial=initial)


def discover(prepared, paths=None):
    return bootstrap_schema(prepared.initial, [], {}, {}, owner_root=prepared.context.owner_root,
        configuration_paths=paths if paths is not None else prepared.context.provider_config_paths)


@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_deferred_siblings_reject_invalid_final_controller_values(tmp_path, predicate, controller_first):
    prepared = deferred_pack(tmp_path, predicate, 0, controller_first)

    with pytest.raises(ValueError, match='expected positive integer'):
        discover(prepared)


@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_deferred_siblings_accept_valid_values_in_either_order(tmp_path, predicate, controller_first):
    prepared = deferred_pack(tmp_path, predicate, 1, controller_first)

    schema = discover(prepared)
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['VALUE'] == 'plain'
    assert resolved.values['MODE'] == 1
    assert schema.fields['MODE'].validation == 'positive'
    assert schema.fields['MODE'].contribution_origin == prepared.late.resolve()
    assert schema.fields['VALUE'].contribution_origin == prepared.late.resolve()


@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_help_rejects_invalid_deferred_siblings_in_either_order(tmp_path, monkeypatch, capsys, predicate, controller_first):
    prepared = deferred_pack(tmp_path, predicate, 0, controller_first)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})

    with patch.dict(os.environ, {}, clear=True):
        status = main(['--help'], base_root=prepared.base, resource_pack_dir=prepared.pack,
            _worktrees_verified=True)

    output = capsys.readouterr()
    assert status == 2
    assert 'Launch error: expected positive integer' in output.err
    assert 'Usage: ./launch.py' not in output.out


@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_help_accepts_valid_deferred_siblings_in_either_order(tmp_path, monkeypatch, capsys, predicate, controller_first):
    prepared = deferred_pack(tmp_path, predicate, 1, controller_first)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})

    with patch.dict(os.environ, {}, clear=True):
        status = main(['--help'], base_root=prepared.base, resource_pack_dir=prepared.pack,
            _worktrees_verified=True)

    output = capsys.readouterr()
    assert status == 0 and not output.err
    assert 'Usage: ./launch.py [OPTIONS] [TEST SELECTORS]' in output.out


def test_deferred_source_order_keeps_later_value_and_owning_file(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.VALUE]\ntype="str"\noverride=true\n'
        'override_if_present=true\ndefault="later"\n')

    schema = discover(prepared, (prepared.late, later))
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['VALUE'] == 'later'
    assert schema.fields['VALUE'].contribution_origin == later.resolve()
    assert schema.fields['MODE'].contribution_origin == prepared.late.resolve()
    assert schema.fields['MODE'].validation == 'positive'


def test_invalid_earlier_source_cannot_be_hidden_by_a_later_override(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 0, False)
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.VALUE]\ntype="str"\noverride=true\n'
        'override_if_present=true\ndefault="later"\n')

    with pytest.raises(ValueError, match='expected positive integer'):
        discover(prepared, (prepared.late, later))


def test_later_source_cannot_weaken_a_deferred_controller(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.MODE]\ntype="int"\noverride=true\n'
        'override_if_present=true\nvalidation="nonnegative"\ndefault=1\n')

    with pytest.raises(ValueError, match='weakens'):
        discover(prepared, (prepared.late, later))


def test_reversing_source_order_changes_the_winner_without_losing_origin(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    earlier = prepared.pack / 'earlier.toml'
    earlier.write_text('version=1\n[fields.VALUE]\ntype="str"\noverride=true\n'
        'override_if_present=true\ndefault="earlier"\n')

    schema = discover(prepared, (earlier, prepared.late))

    assert schema.fields['VALUE'].default == 'plain'
    assert schema.fields['VALUE'].contribution_origin == prepared.late.resolve()
    assert schema.fields['MODE'].validation == 'positive'


def test_absent_deferred_target_stays_inert_with_a_sibling_controller(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 0, False)
    (prepared.pack / 'adapter/cfg/config.toml').write_text('version=1\n[fields.MODE]\n'
        'type="int"\nvalidation="nonnegative"\ndefault=0\n')

    schema = discover(prepared)

    assert 'VALUE' not in schema.fields
    assert schema.fields['MODE'].default == 1
    assert schema.fields['MODE'].validation == 'positive'


def test_deferred_predicate_cannot_request_a_controller_in_a_later_file(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    prepared.late.write_text('version=1\n[fields.VALUE]\ntype="str"\noverride=true\n'
        'override_if_present=true\napplies_when={LATER=true}\n')
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.LATER]\ntype="bool"\ndefault=true\n')

    with pytest.raises(ValueError, match='Unknown controlling field LATER'):
        discover(prepared, (prepared.late, later))
