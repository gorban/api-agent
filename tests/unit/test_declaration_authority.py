"""Explicit field overrides preserve safety while allowing pack presentation."""
import pytest

from api_agent.config.declarations import compose_schema, load_declarations, load_shared_schema, schema_from_dict


def _schema(fields):
    return schema_from_dict({'version': 1, 'fields': fields})


def test_demo_pack_schema_composes_without_importing_factories():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    shared = load_shared_schema(root)
    pack = load_declarations(root / 'demo/cfg/config.toml', base_schema=shared, source='pack_default')
    result = compose_schema(shared, pack, overrides={'UI_NX_APP': 'shell'})
    assert result.fields['UI_NX_APP'].default == 'shell'
    assert result.fields['UI_NX_APP'].default_source == 'pack_default'


def test_omitted_constraints_are_inherited_and_presentation_can_change():
    shared = _schema({'SETTING': {'type': 'int', 'required': True, 'validation': 'positive',
                                  'enum': [1, 2], 'pack_only': True, 'cost_affecting': True,
                                  'acknowledge_default': True, 'review_optional': False}})
    extension = _schema({'SETTING': {'type': 'int', 'override': True, 'default': 2,
                                     'help': 'A local setting', 'acknowledge_default': False,
                                     'review_optional': True}})
    field = compose_schema(shared, extension).fields['SETTING']
    assert (field.required, field.validation, field.enum, field.pack_only, field.cost_affecting) == (
        True, 'positive', (1, 2), True, True)
    assert field.default == 2
    assert field.help == 'A local setting'
    assert not field.acknowledge_default and field.review_optional


@pytest.mark.parametrize('change', [
    {'required': False}, {'validation': None}, {'enum': [1, 2, 3]}, {'enum': []},
    {'allow_empty': True}, {'pack_only': False}, {'cost_affecting': False},
    {'required_when': {'MODE': False}},
])
def test_explicit_weakening_is_rejected(change):
    shared = _schema({'MODE': {'type': 'bool'}, 'SETTING': {
        'type': 'int', 'required': True, 'required_when': {'MODE': True},
        'validation': 'positive', 'enum': [1, 2], 'pack_only': True,
        'cost_affecting': True}})
    extension = schema_from_dict({'version': 1, 'fields': {'SETTING': {
        'type': 'int', 'override': True, **change}}}, base_schema=shared)
    with pytest.raises(ValueError, match='weakens'):
        compose_schema(shared, extension)


def test_tighter_enum_and_new_requiredness_are_supported():
    shared = _schema({'SETTING': {'type': 'str', 'enum': ['a', 'b']}})
    extension = _schema({'SETTING': {'type': 'str', 'override': True,
                                     'required': True, 'enum': ['b']}})
    field = compose_schema(shared, extension).fields['SETTING']
    assert field.required and field.enum == ('b',)


@pytest.mark.parametrize(('data', 'message'), [
    ({'version': 2}, 'version'),
    ({'version': 1, 'fields': []}, 'fields table'),
    ({'version': 1, 'fields': {'bad-name': {'type': 'str'}}}, 'Invalid configuration field'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'required': 'yes'}}}, 'must be boolean'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'override_if_present': True}}}, 'requires override'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'anchor': 'unknown'}}}, 'invalid path anchor'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'path'}}}, 'explicit anchor'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'allow_empty': True}}}, 'allow_empty'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'validation': 'unknown'}}}, 'unknown validation'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'validation': 'positive'}}}, 'positive validation'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'int', 'validation': 'regex'}}}, 'regex validation'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'applies_when': []}}}, 'Predicate'),
    ({'version': 1, 'fields': {'ITEM': {'type': 'str', 'applies_when': {'any': []}}}}, 'Predicate any'),
])
def test_malformed_declarations_are_rejected(data, message):
    with pytest.raises(ValueError, match=message):
        schema_from_dict(data)


def test_compose_rejects_unknown_override_and_missing_override_target():
    shared = _schema({'ITEM': {'type': 'str'}})
    with pytest.raises(ValueError, match='no shared field'):
        compose_schema(shared, _schema({'NEW': {'type': 'str', 'override': True}}))
    with pytest.raises(ValueError, match='Unknown default override'):
        compose_schema(shared, overrides={'ABSENT': 'value'})
    with pytest.raises(ValueError, match='path override'):
        compose_schema(_schema({'FILE': {'type': 'path', 'anchor': 'pack_root'}}),
                       overrides={'FILE': {'value': 'one'}})


def test_deferred_override_skips_absent_field_and_conditional_default_is_checked():
    shared = _schema({'ITEM': {'type': 'int', 'validation': 'positive'}})
    deferred = _schema({'ABSENT': {'type': 'str', 'override': True,
                                   'override_if_present': True}})
    assert 'ABSENT' not in compose_schema(shared, deferred).fields
    extension = _schema({'ITEM': {'type': 'int', 'override': True,
                                  'conditional_defaults': [{'when': {}, 'value': -1}]}})
    with pytest.raises(ValueError, match='positive'):
        compose_schema(shared, extension)


def test_shared_declaration_path_falls_back_to_bundled_copy(tmp_path, monkeypatch):
    import api_agent.config.declarations as declarations
    fake = tmp_path / 'site/api_agent/config/declarations.py'
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(declarations, '__file__', str(fake))
    assert declarations.shared_declaration_path() == tmp_path / 'site/api_agent/cfg/config.toml'


def test_positive_validation_can_strengthen_nonnegative():
    from api_agent.config.declarations import schema_from_dict, compose_schema
    from api_agent.config.resolve import convert_value
    base = schema_from_dict({'version': 1, 'fields': {'COUNT': {'type': 'int', 'validation': 'nonnegative'}}})
    extension = schema_from_dict({'version': 1, 'fields': {'COUNT': {'type': 'int', 'override': True, 'validation': 'positive'}}})

    strengthened = compose_schema(base, extension).fields['COUNT']

    assert convert_value(strengthened, 1, external=False, anchors=None) == 1
    with pytest.raises(ValueError):
        convert_value(strengthened, 0, external=False, anchors=None)
    weaker = schema_from_dict({'version': 1, 'fields': {'COUNT': {'type': 'int', 'override': True, 'validation': 'nonnegative'}}})
    with pytest.raises(ValueError, match='weakens'):
        compose_schema(compose_schema(base, extension), weaker)
