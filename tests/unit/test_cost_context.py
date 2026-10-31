"""Matching fingerprints use explicit non-secret declarations, never credentials."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from api_agent.config.declarations import ConfigSchema, FieldDeclaration, schema_from_dict
from api_agent.testing.cost_context import cost_config, file_fingerprint
from api_agent.telemetry.usage import SuiteUsage, QuotaSnapshot, _snapshot_json
from datetime import datetime, timezone


def test_fingerprint_rejects_nonregular_links_and_oversized_sources(tmp_path):
    source = tmp_path / 'pack.toml'; source.write_bytes(b'pack')
    assert file_fingerprint(source) == sha256(b'pack').hexdigest()
    link = tmp_path / 'link'; link.symlink_to(source)
    assert file_fingerprint(link) is None
    fifo = tmp_path / 'fifo'; os.mkfifo(fifo)
    assert file_fingerprint(fifo) is None
    source.write_bytes(b'x' * 4194305)
    assert file_fingerprint(source) is None


def context(tmp_path):
    pack = tmp_path / 'pack'; pack.mkdir(); (pack / 'pack.toml').write_text('version = 1')
    return SimpleNamespace(base_root=tmp_path, pack_root=pack, owner_root=pack, config_path=pack / 'cfg/config.toml')


def mocked_schema(monkeypatch, fields):
    from api_agent.config import declarations
    from api_agent.providers import loading
    schema = ConfigSchema({field.name:field for field in fields})
    monkeypatch.setattr(declarations, 'load_shared_schema', lambda *a: schema)
    monkeypatch.setattr(declarations, 'load_declarations', lambda *a, **k: ConfigSchema({}))
    monkeypatch.setattr(loading, 'bootstrap_schema', lambda shared, *a, **k: shared)


def test_only_declared_values_affect_identity_and_values_are_not_stored(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    mocked_schema(monkeypatch, [FieldDeclaration('CHOICE', 'str', cost_affecting=True),
                               FieldDeclaration('PATH', 'path', cost_affecting=True),
                               FieldDeclaration('IGNORED', 'str')])
    config = {'CHOICE':'precise', 'PATH':Path('/safe/path'), 'IGNORED':'unchanged'}
    result = cost_config(ctx, config)
    assert result['_COST_PACK_IDENTITY'] == sha256(b'version = 1').hexdigest()
    assert set(result['_COST_REQUEST_SETTINGS']) == {'CHOICE', 'PATH'}
    assert result['_COST_REQUEST_SETTINGS']['CHOICE'] == sha256(b'"precise"').hexdigest()
    config['IGNORED'] = 'changed'
    assert result['_COST_REQUEST_SETTINGS'] == cost_config(ctx, config)['_COST_REQUEST_SETTINGS']
    config['CHOICE'] = 'other'
    assert result['_COST_REQUEST_SETTINGS'] != cost_config(ctx, config)['_COST_REQUEST_SETTINGS']


@pytest.mark.parametrize('field,value', [(FieldDeclaration('API_KEY','str',cost_affecting=True),'secret'),
    (FieldDeclaration('OBJECT','jsonobject',cost_affecting=True),{}),
    (FieldDeclaration('CHOICE','str',cost_affecting=True),None),
    (FieldDeclaration('CHOICE','float',cost_affecting=True),float('nan'))])
def test_missing_or_secret_declarations_cannot_match(tmp_path, monkeypatch, field, value):
    mocked_schema(monkeypatch, [field])
    assert cost_config(context(tmp_path), {field.name:value})['_COST_REQUEST_SETTINGS'] is None


def test_absent_declarations_are_optional_accounting_not_execution_failure(tmp_path):
    ctx = context(tmp_path)
    assert cost_config(ctx, {})['_COST_REQUEST_SETTINGS'] is None


def test_cost_affecting_metadata_validated_and_history_horizon_positive():
    from api_agent.config.declarations import load_shared_schema
    from api_agent.config.resolve import convert_value
    schema = schema_from_dict({'version':1,'fields':{'VALUE':{'type':'str','cost_affecting':True}}})
    assert schema.fields['VALUE'].cost_affecting is True
    with pytest.raises(ValueError,match='must be boolean'):
        schema_from_dict({'version':1,'fields':{'VALUE':{'type':'str','cost_affecting':'true'}}})
    field = load_shared_schema().fields['TEST_COST_HISTORY_DAYS']
    assert field.default == 7
    assert convert_value(field, '3', external=True, anchors=None) == 3
    with pytest.raises(ValueError,match='positive'):
        convert_value(field, '0', external=True, anchors=None)


def test_inference_evidence_is_copied_and_idempotent():
    tracker = SuiteUsage('run')
    first = {'call_id':'call','selection':'model','usage':None}
    assert tracker.record_inference('call','test',first)
    assert not tracker.record_inference('call','test',first)
    first['selection'] = 'other'
    assert tracker.inference_calls['call']['selection'] == 'model'
    with pytest.raises(ValueError,match='identity'):
        tracker.record_inference('call','other-test',first)
    final = {'call_id':'call','selection':'model','usage':{'input_tokens':1}}
    assert tracker.record_inference('call','test',final)
    assert not tracker.record_inference('call','test',final)
    with pytest.raises(ValueError,match='evidence'):
        tracker.record_inference('call','test',{**final,'usage':{'input_tokens':2}})


@pytest.mark.parametrize('step', [0, -1, 101, True, float('nan')])
def test_invalid_precision_is_not_inferred(step):
    with pytest.raises(ValueError,match='percentage step'):
        QuotaSnapshot(1,'opaque',None,datetime.now(timezone.utc),step)


def test_optional_precision_preserves_old_snapshot_shape():
    instant = datetime.now(timezone.utc)
    snapshot = QuotaSnapshot(1,'opaque',None,instant)
    assert set(_snapshot_json(snapshot)) == {'pct_used','window_id','reset_at','captured_at','allocation_id','observation_id'}
    assert _snapshot_json(QuotaSnapshot(1,'opaque',None,instant,.1))['pct_step'] == .1


@pytest.mark.parametrize('key,changed', [('selection','other'), ('started_at','later'), ('attempt',2), ('turn_id','other'), ('turn_kind','approval'), ('call_id','other')])
def test_pending_inference_identity_cannot_be_reassigned(key, changed):
    tracker = SuiteUsage('run')
    evidence = {'call_id':'call','selection':'model','started_at':'now','attempt':1,'turn_id':'turn','turn_kind':'initial','usage':None}
    tracker.record_inference('call','test',evidence)
    with pytest.raises(ValueError,match='identity'):
        tracker.record_inference('call','test',{**evidence,key:changed})
    assert tracker.inference_calls['call'][key] == evidence[key]


def test_provider_fingerprint_is_content_and_symbol_not_location(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    mocked_schema(monkeypatch, [])
    one = tmp_path / 'one.py'; two = tmp_path / 'two.py'
    one.write_text('factory code'); two.write_text('factory code')
    a = cost_config(ctx, {'LLM_PROVIDER':str(one)+':create'})
    assert a['_COST_PROVIDER_IDENTITY'] == cost_config(ctx, {'LLM_PROVIDER':str(two)+':create'})['_COST_PROVIDER_IDENTITY']
    assert a['_COST_PROVIDER_IDENTITY'] != cost_config(ctx, {'LLM_PROVIDER':str(two)+':other'})['_COST_PROVIDER_IDENTITY']
    assert cost_config(ctx, {'LLM_PROVIDER':str(tmp_path/'missing')+':create'})['_COST_PROVIDER_IDENTITY'] is None


def test_inactive_request_field_is_explicitly_distinct_from_missing(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    mocked_schema(monkeypatch, [FieldDeclaration('MODE', 'str'),
        FieldDeclaration('ENDPOINT','str',cost_affecting=True,applies_when={'MODE':'key'})])
    inactive = cost_config(ctx, {'MODE':'subscription'})
    assert inactive['_COST_REQUEST_SETTINGS'] == {'ENDPOINT':sha256(b'inactive').hexdigest()}
    assert cost_config(ctx, {'MODE':'key'})['_COST_REQUEST_SETTINGS'] is None


def test_failed_terminal_inference_cannot_later_be_completed():
    tracker = SuiteUsage('run')
    final = {'call_id':'call','selection':'model','usage':None,'finished_at':'now','outcome':'failed'}
    tracker.record_inference('call','test',final)
    assert not tracker.record_inference('call','test',final)
    with pytest.raises(ValueError,match='evidence'):
        tracker.record_inference('call','test',{**final,'usage':{'input_tokens':1},'outcome':'completed'})
    assert tracker.inference_calls['call']['outcome'] == 'failed'


def test_known_contamination_survives_frozen_accounting(tmp_path):
    from api_agent.testing.recording import RunIdentity
    from api_agent.telemetry.accounting import build_accounting, validate_accounting
    tracker = SuiteUsage('run')
    assert tracker.report()['contaminated'] is False
    tracker.mark_quota_contaminated()
    identity = RunIdentity.create('base', 'integration')
    artifact = build_accounting(identity, (), {}, tracker, ())
    assert artifact['quota']['contaminated'] is True
    assert validate_accounting(artifact) is artifact
    artifact['quota']['contaminated'] = 'yes'
    with pytest.raises(ValueError,match='contamination'):
        validate_accounting(artifact)


@pytest.mark.parametrize('mode,retries,expected', [('integration',None,0), ('integration',5,0),
    ('e2e',None,None), ('e2e',2,2)])
def test_history_records_actual_retry_policy(tmp_path, monkeypatch, mode, retries, expected):
    ctx = context(tmp_path)
    mocked_schema(monkeypatch, [])
    from api_agent.telemetry.accounting import compatibility
    config = {'TUI_MODE':mode, 'E2E_RERUNS':retries}
    resolved = cost_config(ctx, config)
    assert compatibility(resolved)['E2E_RERUNS'] == expected
    assert config['E2E_RERUNS'] == retries


@pytest.mark.parametrize('external', [True, False])
def test_repository_tests_fingerprint_their_own_checkout(tmp_path, external):
    from api_agent.telemetry.accounting import source_fingerprint, build_accounting
    from api_agent.testing.collection import TestIdentity, repository_label
    from api_agent.testing.recording import RunIdentity
    base = tmp_path / 'base'; base.mkdir()
    owner = tmp_path / 'pack' if external else base
    if external:
        owner.mkdir()
    ctx = SimpleNamespace(base_root=base, owner_root=owner)
    (base / 'test.py').write_bytes(b'base suite')
    (owner / 'test.py').write_bytes(b'pack suite' if external else b'base suite')
    tests = [TestIdentity(repository_label(ctx, path)+'::test.py::test_one',
                          repository_label(ctx, path), 'test.py', 'test.py::test_one')
             for path in dict.fromkeys((base, owner))]
    for test, path in zip(tests, dict.fromkeys((base, owner))):
        expected = sha256((path / 'test.py').read_bytes()).hexdigest()
        assert source_fingerprint(test, context=ctx) == expected
        assert source_fingerprint(test) is None
        artifact = build_accounting(RunIdentity.create('base', 'integration'), [test], {}, SuiteUsage('run'), (), context=ctx)
        assert artifact['tests'][0]['source_fingerprint'] == expected
    unknown = TestIdentity('other::test.py::test_one', 'other', 'test.py', 'test.py::test_one')
    assert source_fingerprint(unknown, context=ctx) is None


@pytest.mark.parametrize('allow_empty,present,value,known', [
    (True, True, None, True), (True, False, None, False),
    (False, True, None, False), (False, False, None, False),
    (True, True, 1024, True)])
def test_nullable_cost_identity_requires_explicit_admitted_value(tmp_path, monkeypatch, allow_empty, present, value, known):
    mocked_schema(monkeypatch, [FieldDeclaration('CAP', 'int', cost_affecting=True, allow_empty=allow_empty)])
    result = cost_config(context(tmp_path), {'CAP': value} if present else {})['_COST_REQUEST_SETTINGS']
    if known:
        import json
        assert result == {'CAP': sha256(json.dumps(value).encode()).hexdigest()}
    else:
        assert result is None


@pytest.mark.parametrize('name,kind', [('API_KEY', 'str'), ('OBJECT', 'jsonobject')])
def test_nullable_secret_or_object_never_becomes_cost_identity(tmp_path, monkeypatch, name, kind):
    mocked_schema(monkeypatch, [FieldDeclaration(name, kind, cost_affecting=True, allow_empty=True)])
    assert cost_config(context(tmp_path), {name: None})['_COST_REQUEST_SETTINGS'] is None


def test_real_workflow_cap_profile_distinguishes_null_and_explicit_cap():
    from test_test_profiles import configuration, CASES, ROOT
    from api_agent.testing.test_profiles import resolve_test_profiles
    from api_agent.runtime.resource_pack import load_launch_context
    config = configuration()
    context = load_launch_context(ROOT)
    rows = resolve_test_profiles(CASES, config.values, owner_root=ROOT, fields=config.fields)
    ordinary = cost_config(context, rows[CASES[0].id]['config'])['_COST_REQUEST_SETTINGS']
    workflow = cost_config(context, rows[CASES[-1].id]['config'])['_COST_REQUEST_SETTINGS']
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    assert ordinary is not None and workflow is not None
    assert ordinary[key] == sha256(b'null').hexdigest()
    assert workflow[key] == sha256(b'1024').hexdigest()
    assert ordinary[key] != workflow[key]


@pytest.mark.parametrize('changes', [dict(type='str'), dict(sensitive=True),
    dict(validation=''), dict(allow_empty=False), dict(type='jsonobject'),
    dict(name='OTHER_MAX_OUTPUT_TOKENS'), dict(name='API_TOKEN'),
    dict(name='CAP', sensitive=True)])
def test_workflow_count_cost_exception_does_not_admit_other_declarations(tmp_path, monkeypatch, changes):
    options = dict(name='LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS', type='int',
                   cost_affecting=True, validation='positive', allow_empty=True)
    options.update(changes)
    field = FieldDeclaration(**options)
    mocked_schema(monkeypatch, [field])
    assert cost_config(context(tmp_path), {field.name: 1024})['_COST_REQUEST_SETTINGS'] is None


@pytest.mark.parametrize('value', [None, 1, 1024, 1048576])
def test_exact_shared_workflow_cap_cost_hash_is_not_mistaken_for_credentials(tmp_path, monkeypatch, value):
    field = FieldDeclaration('LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS', 'int',
                            cost_affecting=True, validation='positive', allow_empty=True)
    mocked_schema(monkeypatch, [field])
    import json
    assert cost_config(context(tmp_path), {field.name: value})['_COST_REQUEST_SETTINGS'] == {
        field.name: sha256(json.dumps(value).encode()).hexdigest()}


@pytest.mark.parametrize('value', [True, False, 'synthetic-secret', 1024.0, {}, [], 0, -1, 1048577])
def test_exact_workflow_cap_never_hashes_noncount_or_out_of_range_values(tmp_path, monkeypatch, value):
    field = FieldDeclaration('LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS', 'int',
                            cost_affecting=True, validation='positive', allow_empty=True)
    mocked_schema(monkeypatch, [field])
    assert cost_config(context(tmp_path), {field.name: value})['_COST_REQUEST_SETTINGS'] is None


def test_explicit_sensitive_string_cannot_become_cost_identity(tmp_path, monkeypatch):
    mocked_schema(monkeypatch, [FieldDeclaration('CHOICE', 'str', cost_affecting=True, sensitive=True)])
    assert cost_config(context(tmp_path), {'CHOICE': 'synthetic-secret'})['_COST_REQUEST_SETTINGS'] is None


@pytest.mark.parametrize('value', [1, 200000, 1048576])
def test_shared_output_ceiling_is_a_validated_cost_setting(tmp_path, monkeypatch, value):
    import json
    field = FieldDeclaration('LLM_MAX_OUTPUT_TOKENS', 'int', cost_affecting=True, validation='positive')
    mocked_schema(monkeypatch, [field])
    assert cost_config(context(tmp_path), {field.name: value})['_COST_REQUEST_SETTINGS'] == {
        field.name: sha256(json.dumps(value).encode()).hexdigest()}


@pytest.mark.parametrize('value', [None, True, 'synthetic-secret', 200000.0, 0, -1, 1048577])
def test_shared_output_ceiling_rejects_noncount_cost_values(tmp_path, monkeypatch, value):
    field = FieldDeclaration('LLM_MAX_OUTPUT_TOKENS', 'int', cost_affecting=True, validation='positive')
    mocked_schema(monkeypatch, [field])
    assert cost_config(context(tmp_path), {field.name: value})['_COST_REQUEST_SETTINGS'] is None


@pytest.mark.parametrize('changes', [dict(type='str'), dict(sensitive=True),
    dict(validation=''), dict(allow_empty=True), dict(type='jsonobject'), dict(name='OTHER_MAX_OUTPUT_TOKENS')])
def test_shared_output_ceiling_exception_requires_exact_count_declaration(tmp_path, monkeypatch, changes):
    options = dict(name='LLM_MAX_OUTPUT_TOKENS', type='int', cost_affecting=True, validation='positive')
    options.update(changes)
    field = FieldDeclaration(**options)
    mocked_schema(monkeypatch, [field])
    assert cost_config(context(tmp_path), {field.name: 200000})['_COST_REQUEST_SETTINGS'] is None


def test_shared_output_ceiling_changes_cost_compatibility():
    from test_test_profiles import configuration, ROOT
    from api_agent.runtime.resource_pack import load_launch_context
    launch = load_launch_context(ROOT)
    values = configuration().values
    lower = cost_config(launch, dict(values, LLM_MAX_OUTPUT_TOKENS=500))['_COST_REQUEST_SETTINGS']
    higher = cost_config(launch, dict(values, LLM_MAX_OUTPUT_TOKENS=200000))['_COST_REQUEST_SETTINGS']
    assert lower is not None and higher is not None
    assert lower['LLM_MAX_OUTPUT_TOKENS'] != higher['LLM_MAX_OUTPUT_TOKENS']
