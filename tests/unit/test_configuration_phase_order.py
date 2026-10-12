"""Source order and complete schemas are independent of discovery phase."""
from pathlib import Path
from unittest.mock import patch
import os

import pytest

from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config
from api_agent.providers.loading import contribution_schema, provider_schema
from api_agent.runtime.bootstrap import main
from test_deferred_contributions import PREDICATES, deferred_pack, discover, mixed_pack


def ordered_pack(tmp_path, first_deferred, last_deferred, *, tightening=False):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    with (prepared.pack / 'config.toml').open('a') as stream:
        stream.write('[fields.VALUE]\ntype="str"\ndefault="base"\n'
            '[fields.COUNT]\ntype="int"\nvalidation="nonnegative"\ndefault=0\n')
    (prepared.pack / 'adapter/cfg/config.toml').write_text('version=1\n')
    name, kind, earlier, later = ('COUNT', 'int', '0', '1') if tightening else ('VALUE', 'str', '"first"', '"second"')
    first, last = prepared.pack / 'first.toml', prepared.pack / 'second.toml'
    first_flag = 'override_if_present=true\n' if first_deferred else ''
    last_flag = 'override_if_present=true\n' if last_deferred else ''
    last_rule = 'validation="positive"\n' if tightening else ''
    first.write_text(f'version=1\n[fields.{name}]\ntype="{kind}"\noverride=true\n{first_flag}default={earlier}\n')
    last.write_text(f'version=1\n[fields.{name}]\ntype="{kind}"\noverride=true\n{last_flag}{last_rule}default={later}\n')
    (prepared.pack / 'pack.toml').write_text('version=1\nid="fixture"\nconfig="config.toml"\n'
        '[providers]\nconfigs=["first.toml","second.toml"]\n')
    shared = load_shared_schema(prepared.base)
    prepared.initial = compose_schema(shared, load_declarations(prepared.context.config_path, base_schema=shared))
    return prepared, first, last


@pytest.mark.parametrize('first_deferred', [False, True])
@pytest.mark.parametrize('last_deferred', [False, True])
def test_later_source_wins_in_all_phase_pairings(tmp_path, first_deferred, last_deferred):
    prepared, first, last = ordered_pack(tmp_path, first_deferred, last_deferred)

    schema = discover(prepared, (first, last))
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['VALUE'] == 'second'
    assert schema.fields['VALUE'].contribution_origin == last.resolve()


@pytest.mark.parametrize('first_deferred', [False, True])
@pytest.mark.parametrize('last_deferred', [False, True])
def test_later_tightening_preserves_valid_earlier_constraints(tmp_path, first_deferred, last_deferred):
    prepared, first, last = ordered_pack(tmp_path, first_deferred, last_deferred, tightening=True)

    schema = discover(prepared, (first, last))
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['COUNT'] == 1
    assert schema.fields['COUNT'].validation == 'positive'
    assert schema.fields['COUNT'].contribution_origin == last.resolve()


@pytest.mark.parametrize('first_deferred', [False, True])
@pytest.mark.parametrize('last_deferred', [False, True])
def test_help_accepts_valid_source_tightening_across_phases(tmp_path, monkeypatch, capsys, first_deferred, last_deferred):
    prepared, _, _ = ordered_pack(tmp_path, first_deferred, last_deferred, tightening=True)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})

    with patch.dict(os.environ, {}, clear=True):
        status = main(['--help'], base_root=prepared.base, resource_pack_dir=prepared.pack,
            _worktrees_verified=True)

    output = capsys.readouterr()
    assert status == 0 and not output.err
    assert 'Usage: ./launch.py [OPTIONS] [TEST SELECTORS]' in output.out


def direct_schema(prepared, api):
    if api == 'compose':
        return compose_schema(prepared.initial, load_declarations(prepared.late, base_schema=prepared.initial))
    if api == 'contribution':
        return contribution_schema(prepared.late, prepared.initial)
    path = prepared.pack / 'adapter/cfg/config.toml'
    path.write_text(prepared.late.read_text())
    return provider_schema(str(path.parent.parent / 'provider.py') + ':factory', prepared.initial)


@pytest.mark.parametrize('api', ['compose', 'contribution', 'provider'])
@pytest.mark.parametrize('predicate', PREDICATES)
@pytest.mark.parametrize('value', [1, '""'])
def test_direct_new_fields_reject_dropped_controller_targets(tmp_path, api, predicate, value):
    prepared = mixed_pack(tmp_path, predicate, value, False, new_value=True, inherited=True)

    with pytest.raises(ValueError, match='Unknown controlling field MODE'):
        direct_schema(prepared, api)


@pytest.mark.parametrize('api', ['compose', 'contribution', 'provider'])
@pytest.mark.parametrize('predicate', PREDICATES)
def test_direct_new_fields_accept_actual_controller_targets(tmp_path, api, predicate):
    prepared = mixed_pack(tmp_path, predicate, 1, False, new_value=True, inherited=True)
    shared = load_shared_schema(prepared.base)
    known = load_declarations(prepared.pack / 'adapter/cfg/config.toml', base_schema=shared)
    prepared.initial = compose_schema(prepared.initial, known)

    schema = direct_schema(prepared, api)
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['MODE'] == 1 and resolved.values['VALUE'] == 'plain'


@pytest.mark.parametrize('predicate', PREDICATES)
def test_prior_pending_controller_remains_lookup_only_across_files(tmp_path, predicate):
    prepared = mixed_pack(tmp_path, predicate, 1, False, new_value=True, inherited=True)
    text = prepared.late.read_text()
    controller = '[fields.MODE]' + text.split('[fields.MODE]', 1)[1]
    dependent = text.split('[fields.MODE]', 1)[0]
    prepared.late.write_text('version=1\n' + controller)
    later = prepared.pack / 'later.toml'
    later.write_text(dependent)

    schema = discover(prepared, (prepared.late, later))
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['VALUE'] == 'plain'
    assert resolved.values['MODE'] == 1
    assert schema.fields['VALUE'].contribution_origin == later.resolve()


def test_later_ordinary_introduction_is_not_promoted_ahead_of_its_source(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    (prepared.pack / 'adapter/cfg/config.toml').write_text('version=1\n')
    prepared.late.write_text('version=1\n[fields.NEW_VALUE]\ntype="int"\n'
        'override=true\noverride_if_present=true\ndefault=2\n')
    later = prepared.pack / 'later.toml'
    later.write_text('version=1\n[fields.NEW_VALUE]\ntype="int"\ndefault=1\n')

    schema = discover(prepared, (prepared.late, later))

    assert schema.fields['NEW_VALUE'].default == 1
    assert schema.fields['NEW_VALUE'].contribution_origin == later.resolve()


def test_promoted_provider_target_can_depend_on_its_actual_sibling(tmp_path):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    prepared.late.write_text('version=1\n[fields.VALUE]\ntype="str"\n'
        'override=true\noverride_if_present=true\ndefault="later"\n')
    (prepared.pack / 'adapter/cfg/config.toml').write_text('version=1\n'
        '[fields.VALUE]\ntype="str"\ndefault="provider"\napplies_when={MODE=1}\n'
        '[fields.MODE]\ntype="int"\ndefault=1\n')

    schema = discover(prepared)
    resolved = resolve_config(schema, environ={}, dotenv={}, anchors=prepared.context.anchors)

    assert resolved.values['VALUE'] == 'later'
    assert schema.fields['MODE'].default == 1
    assert schema.fields['VALUE'].contribution_origin == prepared.late.resolve()


def selector_owner_pack(tmp_path, *, alternate=False):
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    child = prepared.pack / 'adapter/child'
    (child / 'cfg').mkdir(parents=True)
    (child / 'provider.py').write_text("raise AssertionError('factory must stay inert')\n")
    (child / 'cfg/config.toml').write_text('version=1\n[fields.CHILD_REQUIRED]\ntype="str"\nrequired=true\n')
    (prepared.pack / 'adapter/cfg/config.toml').write_text('version=1\n'
        '[fields.UPSTREAM_API_PROVIDER]\ntype="str"\ndefault="child/provider.py:factory"\n')
    target = 'adapter/child/provider.py:factory'
    if alternate:
        (prepared.pack / 'alternate.py').write_text("raise AssertionError('factory must stay inert')\n")
        target = 'alternate.py:factory'
    prepared.late.write_text('version=1\n[fields.UPSTREAM_API_PROVIDER]\ntype="str"\n'
        f'override=true\noverride_if_present=true\ndefault="{target}"\n')
    return prepared


def test_equivalent_selector_paths_preserve_winning_owner_without_cycle(tmp_path):
    prepared = selector_owner_pack(tmp_path)

    schema = discover(prepared)

    assert schema.fields['UPSTREAM_API_PROVIDER'].default == 'adapter/child/provider.py:factory'
    assert schema.fields['UPSTREAM_API_PROVIDER'].contribution_origin == prepared.late.resolve()
    assert schema.fields['CHILD_REQUIRED'].required


def test_deferred_selector_still_rejects_a_different_canonical_target(tmp_path):
    from api_agent.providers.loading import ProviderError
    prepared = selector_owner_pack(tmp_path, alternate=True)

    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        discover(prepared)


@pytest.mark.parametrize('environment,override', [
    ({'UPSTREAM_API_PROVIDER': 'child/provider.py:factory'}, {}),
    ({}, {'UPSTREAM_API_PROVIDER': 'child/provider.py:factory'}),
])
def test_explicit_selector_keeps_its_discovered_provider_scope(tmp_path, environment, override):
    from api_agent.providers.loading import bootstrap_schema
    prepared = selector_owner_pack(tmp_path)

    schema = bootstrap_schema(prepared.initial, [], environment, {}, override,
        owner_root=prepared.context.owner_root, configuration_paths=(prepared.late,))
    resolved = resolve_config(schema, environ=environment, dotenv={}, overrides=override,
        anchors=prepared.context.anchors)

    assert schema.fields['CHILD_REQUIRED'].required
    assert schema.fields['UPSTREAM_API_PROVIDER'].contribution_origin == prepared.late.resolve()
    assert resolved.values['UPSTREAM_API_PROVIDER'] == 'child/provider.py:factory'


@pytest.mark.parametrize('target', ['missing.py:factory', 'malformed-selector'])
def test_deferred_selector_rejects_invalid_target_with_controlled_cycle_error(tmp_path, target):
    from api_agent.providers.loading import ProviderError
    prepared = selector_owner_pack(tmp_path)
    prepared.late.write_text('version=1\n[fields.UPSTREAM_API_PROVIDER]\ntype="str"\n'
        f'override=true\noverride_if_present=true\ndefault="{target}"\n')

    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        discover(prepared)


EXPLICIT_SCOPE_INPUTS = [
    ([], {'LLM_PROVIDER': 'llm.py:factory'}, {}, {}),
    ([], {}, {'LLM_PROVIDER': 'llm.py:factory'}, {}),
    (['--llm-provider=llm.py:factory'], {}, {}, {}),
    ([], {}, {}, {'LLM_PROVIDER': 'llm.py:factory'}),
]


def changed_selector_owner_pack(tmp_path, *, shadow=False):
    from api_agent.runtime.resource_pack import load_launch_context
    prepared = deferred_pack(tmp_path, PREDICATES[0], 1, False)
    (prepared.pack / 'llm.py').write_text("raise AssertionError('factory must stay inert')\n")
    api = prepared.pack / 'api'
    (api / 'cfg').mkdir(parents=True)
    (api / 'provider.py').write_text("raise AssertionError('factory must stay inert')\n")
    if shadow:
        (api / 'llm.py').write_text("raise AssertionError('shadow factory must stay inert')\n")
    (api / 'cfg/config.toml').write_text('version=1\n[fields.LLM_PROVIDER]\ntype="str"\n'
        'override=true\ndefault="../llm.py:factory"\n'
        '[fields.API_REQUIRED]\ntype="str"\nrequired=true\n')
    (prepared.pack / 'config.toml').write_text('version=1\n[fields.LLM_PROVIDER]\ntype="str"\n'
        f'override=true\ndefault="{prepared.pack / "llm.py"}:factory"\n'
        '[fields.API_PROVIDER]\ntype="str"\noverride=true\ndefault="api/provider.py:factory"\n')
    (prepared.pack / 'pack.toml').write_text('version=1\nid="fixture"\nconfig="config.toml"\n')
    prepared.context = load_launch_context(prepared.base, prepared.pack)
    shared = load_shared_schema(prepared.base)
    prepared.initial = compose_schema(shared, load_declarations(prepared.context.config_path, base_schema=shared))
    return prepared


@pytest.mark.parametrize('argv,environment,dotenv,override', EXPLICIT_SCOPE_INPUTS,
                         ids=['environment', 'dotenv', 'cli', 'interactive'])
@pytest.mark.parametrize('shadow', [False, True], ids=['missing-shadow', 'existing-shadow'])
def test_explicit_selector_scope_survives_later_default_owner_change(tmp_path, argv, environment, dotenv, override, shadow):
    from api_agent.providers.loading import bootstrap_schema
    prepared = changed_selector_owner_pack(tmp_path, shadow=shadow)

    schema = bootstrap_schema(prepared.initial, ['--help', *argv], environment, dotenv, override,
        owner_root=prepared.context.owner_root)
    resolved = resolve_config(schema, argv=argv, environ=environment, dotenv=dotenv,
        overrides=override, anchors=prepared.context.anchors)

    assert schema.fields['API_REQUIRED'].required
    assert schema.fields['LLM_PROVIDER'].default == str(prepared.pack / 'llm.py') + ':factory'
    assert schema.fields['LLM_PROVIDER'].contribution_origin == (prepared.pack / 'api/cfg/config.toml').resolve()
    assert resolved.values['LLM_PROVIDER'] == 'llm.py:factory'


def test_actual_help_retains_explicit_relative_selector_discovery_scope(tmp_path, monkeypatch, capsys):
    prepared = changed_selector_owner_pack(tmp_path, shadow=True)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})

    with patch.dict(os.environ, {}, clear=True):
        status = main(['--help', '--llm-provider=llm.py:factory'], base_root=prepared.base,
            resource_pack_dir=prepared.pack, _worktrees_verified=True)

    output = capsys.readouterr()
    assert status == 0 and not output.err
    assert 'Usage: ./launch.py [OPTIONS] [TEST SELECTORS]' in output.out


def test_equivalent_default_selector_retains_its_contributing_scope(tmp_path):
    from api_agent.providers.loading import bootstrap_schema
    prepared = changed_selector_owner_pack(tmp_path, shadow=True)

    schema = bootstrap_schema(prepared.initial, [], {}, {}, owner_root=prepared.context.owner_root)

    assert schema.fields['API_REQUIRED'].required
    assert schema.fields['LLM_PROVIDER'].default == str(prepared.pack / 'llm.py') + ':factory'
    assert schema.fields['LLM_PROVIDER'].contribution_origin == (prepared.pack / 'api/cfg/config.toml').resolve()


@pytest.mark.parametrize('argv,environment,dotenv,override', EXPLICIT_SCOPE_INPUTS,
                         ids=['environment', 'dotenv', 'cli', 'interactive'])
def test_captured_explicit_scope_still_rejects_a_changed_canonical_target(tmp_path, monkeypatch, argv, environment, dotenv, override):
    from dataclasses import replace
    from api_agent.config import resolve as resolver
    from api_agent.providers.loading import ProviderError, bootstrap_schema
    prepared = changed_selector_owner_pack(tmp_path, shadow=True)
    original = resolver.resolve_config

    def changed_resolution(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, values={**result.values, 'LLM_PROVIDER': 'api/llm.py:factory'})

    monkeypatch.setattr(resolver, 'resolve_config', changed_resolution)

    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        bootstrap_schema(prepared.initial, argv, environment, dotenv, override,
            owner_root=prepared.context.owner_root)
