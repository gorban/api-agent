"""Deferred declarations retain sibling context and ordered source authority."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import os

import pytest

from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config, schema_from_dict
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
    assert 'Usage: ./launch.sh' not in output.out


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
    assert 'Usage: ./launch.sh [OPTIONS] [TEST SELECTORS]' in output.out


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


def mixed_pack(tmp_path, predicate, value, controller_first, *, new_value=False, inherited=False):
    prepared = deferred_pack(tmp_path, predicate, value, controller_first)
    controller_rule = 'positive' if inherited else 'nonnegative'
    controller_default = 1 if inherited else 0
    (prepared.pack / 'adapter/cfg/config.toml').write_text(
        f'version=1\n[fields.MODE]\ntype="int"\nvalidation="{controller_rule}"\ndefault={controller_default}\n')
    if not new_value:
        with (prepared.pack / 'config.toml').open('a') as stream:
            stream.write('[fields.VALUE]\ntype="str"\ndefault="initial"\n')
    controlled_head = '[fields.VALUE]\ntype="str"\noverride=true\noverride_if_present=true\n'
    immediate_head = '[fields.VALUE]\ntype="str"\n' + ('' if new_value else 'override=true\n')
    content = prepared.late.read_text().replace(controlled_head, immediate_head)
    if inherited:
        content = content.replace('validation="positive"\n', '')
    prepared.late.write_text(content)
    shared = load_shared_schema(prepared.base)
    prepared.initial = compose_schema(shared, load_declarations(prepared.context.config_path, base_schema=shared))
    return prepared


@pytest.mark.parametrize('new_value', [False, True])
@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_mixed_siblings_accept_valid_predicates(tmp_path, predicate, controller_first, new_value):
    prepared = mixed_pack(tmp_path, predicate, 1, controller_first, new_value=new_value)

    schema = discover(prepared)
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['VALUE'] == 'plain' and resolved.values['MODE'] == 1
    assert schema.fields['MODE'].validation == 'positive'
    assert schema.fields['VALUE'].contribution_origin == prepared.late.resolve()


@pytest.mark.parametrize('new_value', [False, True])
@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_mixed_siblings_reject_invalid_inherited_controller_values(tmp_path, predicate, controller_first, new_value):
    prepared = mixed_pack(tmp_path, predicate, 0, controller_first, new_value=new_value, inherited=True)

    with pytest.raises(ValueError, match='expected positive integer'):
        discover(prepared)


@pytest.mark.parametrize('new_value', [False, True])
@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_help_accepts_valid_mixed_siblings(tmp_path, monkeypatch, capsys, predicate, controller_first, new_value):
    prepared = mixed_pack(tmp_path, predicate, 1, controller_first, new_value=new_value)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})

    with patch.dict(os.environ, {}, clear=True):
        status = main(['--help'], base_root=prepared.base, resource_pack_dir=prepared.pack,
            _worktrees_verified=True)

    output = capsys.readouterr()
    assert status == 0 and not output.err
    assert 'Usage: ./launch.sh [OPTIONS] [TEST SELECTORS]' in output.out


@pytest.mark.parametrize('new_value', [False, True])
@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_help_rejects_invalid_mixed_siblings(tmp_path, monkeypatch, capsys, predicate, controller_first, new_value):
    prepared = mixed_pack(tmp_path, predicate, 0, controller_first, new_value=new_value, inherited=True)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})

    with patch.dict(os.environ, {}, clear=True):
        status = main(['--help'], base_root=prepared.base, resource_pack_dir=prepared.pack,
            _worktrees_verified=True)

    output = capsys.readouterr()
    assert status == 2 and 'Launch error: expected positive integer' in output.err
    assert 'Usage: ./launch.sh' not in output.out


def test_pending_controller_context_is_lookup_only(tmp_path):
    from api_agent.providers.loading import contribution_schema
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)
    pending = []

    immediate = contribution_schema(prepared.late, prepared.initial, deferred=pending)

    assert 'MODE' not in immediate.fields
    assert immediate.fields['VALUE'].default == 'plain'
    assert len(pending) == 1 and set(pending[0].overrides.fields) == {'MODE'}
    assert pending[0].source.fields['VALUE'].default == immediate.fields['VALUE'].default
    assert pending[0].introduced == frozenset()
    assert not pending[0].provider


def test_direct_composition_does_not_grant_pending_controller_context(tmp_path):
    from api_agent.providers.loading import contribution_schema
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)

    with pytest.raises(ValueError, match='Unknown controlling field MODE'):
        contribution_schema(prepared.late, prepared.initial)


def test_mixed_dependency_rejects_an_absent_controller_target(tmp_path):
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)
    (prepared.pack / 'adapter/cfg/config.toml').write_text('version=1\n')

    with pytest.raises(ValueError, match='Unknown controlling field MODE'):
        discover(prepared)


def test_later_value_cannot_hide_a_mixed_invalid_controller_predicate(tmp_path):
    prepared = mixed_pack(tmp_path, PREDICATES[0], 0, False, inherited=True)
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.VALUE]\ntype="str"\noverride=true\ndefault="later"\n')

    with pytest.raises(ValueError, match='expected positive integer'):
        discover(prepared, (prepared.late, later))


def test_revalidation_does_not_replay_older_defaults_or_later_self_constraints(tmp_path):
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)
    with (prepared.pack / 'config.toml').open('a') as stream:
        stream.write('[fields.COUNT]\ntype="int"\nvalidation="nonnegative"\ndefault=0\n')
    with prepared.late.open('a') as stream:
        stream.write('[fields.COUNT]\ntype="int"\noverride=true\ndefault=0\n')
    shared = load_shared_schema(prepared.base)
    prepared.initial = compose_schema(shared, load_declarations(prepared.context.config_path, base_schema=shared))
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.COUNT]\ntype="int"\noverride=true\nvalidation="positive"\ndefault=1\n')

    schema = discover(prepared, (prepared.late, later))

    assert schema.fields['COUNT'].default == 1
    assert schema.fields['COUNT'].validation == 'positive'
    assert schema.fields['COUNT'].contribution_origin == later.resolve()
    assert schema.fields['VALUE'].default == 'plain'


def test_mixed_selector_condition_cannot_change_the_discovered_provider(tmp_path):
    from api_agent.providers.loading import ProviderError
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)
    alternate = prepared.pack / 'alternate.py'
    alternate.write_text("raise AssertionError('an alternate factory must not execute')\n")
    with prepared.late.open('a') as stream:
        stream.write('[fields.LLM_PROVIDER]\ntype="str"\noverride=true\n'
            'default="adapter/provider.py:factory"\n'
            f'conditional_defaults=[{{when={{MODE=1}},value="{alternate}:factory"}}]\n')

    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        discover(prepared)


def earlier_predicate_pack(tmp_path, immediate):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 0, False)
    flag = '' if immediate else 'override_if_present=true\n'
    prepared.late.write_text('version=1\n[fields.VALUE]\ntype="str"\noverride=true\n' + flag +
        'default="plain"\napplies_when={MODE=0}\n')
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.MODE]\ntype="int"\noverride=true\n'
        'override_if_present=true\nvalidation="positive"\ndefault=1\n')
    extra = {'MODE': {'type': 'int', 'validation': 'nonnegative', 'default': 0}}
    provider_fields = 'version=1\n[fields.VALUE]\ntype="str"\n'
    if immediate:
        extra['VALUE'] = {'type': 'str'}
        provider_fields = 'version=1\n'
    additional = schema_from_dict({'version': 1, 'fields': extra})
    fields = dict(prepared.initial.fields) | dict(additional.fields)
    prepared.initial = type(prepared.initial)(fields)
    (prepared.pack / 'adapter/cfg/config.toml').write_text(provider_fields)
    return prepared, later


@pytest.mark.parametrize('immediate', [False, True])
def test_later_controller_tightening_rechecks_earlier_predicates(tmp_path, immediate):
    prepared, later = earlier_predicate_pack(tmp_path, immediate)

    with pytest.raises(ValueError, match='expected positive integer'):
        discover(prepared, (prepared.late, later))


def test_pending_lookup_cannot_weaken_an_actual_controller():
    shared = schema_from_dict({'version': 1, 'fields': {
        'MODE': {'type': 'int', 'validation': 'positive', 'default': 1},
        'VALUE': {'type': 'str', 'default': 'initial'}}})
    override = replace(shared.fields['VALUE'], override=True, applies_when={'MODE': 0})
    extension = type(shared)({'VALUE': override})
    context = schema_from_dict({'version': 1, 'fields': {
        'MODE': {'type': 'int', 'validation': 'nonnegative', 'default': 0}}})

    with pytest.raises(ValueError, match='expected positive integer'):
        compose_schema(shared, extension, predicate_context=context)


def blank_controller_pack(tmp_path, kind, predicate, controller_first, *, allow_empty=True, new_value=False):
    prepared = mixed_pack(tmp_path, predicate, 1, controller_first, new_value=new_value)
    anchor = 'anchor="owner_root"\n' if kind == 'path' else ''
    (prepared.pack / 'adapter/cfg/config.toml').write_text(
        f'version=1\n[fields.MODE]\ntype="{kind}"\n{anchor}allow_empty={str(allow_empty).lower()}\n')
    authority = '' if new_value else 'override=true\n'
    controlled = '[fields.VALUE]\ntype="str"\n' + authority + 'default="plain"\n'
    controlled += predicate.format(value='""') + '\n'
    # Invalid predicate controls have a valid controller default, so rejection
    # proves its predicate conversion rather than another invalid default.
    valid_default = '0' if kind == 'int' else '"valid/path"'
    default = '""' if allow_empty else valid_default
    controller = f'[fields.MODE]\ntype="{kind}"\noverride=true\noverride_if_present=true\n{anchor}default={default}\n'
    prepared.late.write_text('version=1\n' + (controller + controlled if controller_first else controlled + controller))
    return prepared


@pytest.mark.parametrize('new_value', [False, True])
@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
@pytest.mark.parametrize('kind', ['int', 'path'])
def test_mixed_empty_controller_predicates_inherit_selected_constraints(tmp_path, kind, predicate, controller_first, new_value):
    prepared = blank_controller_pack(tmp_path, kind, predicate, controller_first, new_value=new_value)

    schema = discover(prepared)
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert schema.fields['MODE'].allow_empty
    assert 'allow_empty' not in schema.fields['MODE'].declared_keys
    assert resolved.values['MODE'] in (None, '')
    assert schema.fields['VALUE'].default == 'plain'
    assert schema.fields['VALUE'].contribution_origin == prepared.late.resolve()
    assert resolved.values['VALUE'] == 'plain'


@pytest.mark.parametrize('controller_first', [False, True])
@pytest.mark.parametrize('predicate', PREDICATES)
@pytest.mark.parametrize(('kind', 'message'), [('int', 'expected int'), ('path', 'expected nonempty path')])
def test_mixed_empty_controller_predicate_requires_actual_empty_permission(tmp_path, kind, message, predicate, controller_first):
    prepared = blank_controller_pack(tmp_path, kind, predicate, controller_first, allow_empty=False)

    with pytest.raises(ValueError, match=message):
        discover(prepared)


@pytest.mark.parametrize('kind', ['int', 'path'])
def test_pending_controller_exemptions_follow_nested_predicates(tmp_path, kind):
    predicate = 'conditional_defaults=[{{when={{any=[{{MODE={value}}}]}},value="plain"}}]'
    prepared = blank_controller_pack(tmp_path, kind, predicate, False)

    schema = discover(prepared)

    assert schema.fields['VALUE'].conditional_defaults[0]['when'] == {'any': [{'MODE': ''}]}
    assert schema.fields['MODE'].allow_empty


def test_malformed_mixed_predicate_is_not_deferred(tmp_path):
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)
    prepared.late.write_text(prepared.late.read_text().replace(
        'applies_when = { MODE = 1 }', 'applies_when = { any = [] }'))

    with pytest.raises(ValueError, match='Predicate any requires a nonempty array'):
        discover(prepared)


def test_pending_controller_does_not_defer_the_immediate_default(tmp_path):
    prepared = mixed_pack(tmp_path, PREDICATES[0], 1, False)
    prepared.late.write_text(prepared.late.read_text().replace('default="plain"', 'default=1'))

    with pytest.raises(ValueError, match='expected str'):
        discover(prepared)


@pytest.mark.parametrize('expected', ['', 1])
def test_lookup_context_cannot_claim_a_non_deferred_missing_controller(expected):
    shared = schema_from_dict({'version': 1, 'fields': {'VALUE': {'type': 'str'}}})
    extension = type(shared)({'VALUE': replace(shared.fields['VALUE'], override=True,
        applies_when={'MODE': expected})})
    context = schema_from_dict({'version': 1, 'fields': {'MODE': {'type': 'int'}}})

    with pytest.raises(ValueError, match='Unknown controlling field MODE'):
        compose_schema(shared, extension, predicate_context=context)


@pytest.mark.parametrize(('predicate', 'values', 'expected'), [
    ({'MODE': ''}, {'MODE': None}, True),
    ({'MODE': ['', 1]}, {'MODE': None}, True),
    ({'any': [{'MODE': ''}]}, {'MODE': None}, True),
    ({'MODE': ''}, {}, False),
    ({'MODE': ''}, {'MODE': 0}, False),
    ({'MODE': 0}, {'MODE': None}, False),
])
def test_empty_integer_predicate_matching_preserves_missing_and_nonempty_values(predicate, values, expected):
    from api_agent.config.resolve import _matches

    assert _matches(predicate, values) is expected
