"""Selected providers can declare account selection without a secondary role."""
import pytest
from api_agent.config import compose_schema, load_shared_schema, resolve_config, schema_from_dict
from api_agent.runtime.bootstrap import generated_help

def selected_schema():
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_USER_INDEX': {
        'type': 'int', 'validation': 'nonnegative', 'acknowledge_default': False,
        'persist_default': False}}}, base_schema=shared, source='pack_default')
    return compose_schema(shared, extension)

def test_bare_core_does_not_expose_provider_selection():
    shared = load_shared_schema()
    assert 'UNATTENDED_USER_INDEX' not in shared.fields
    assert '--unattended-user-index' not in generated_help(shared)

def test_llm_only_declaration_exposes_generic_index_without_secondary_role():
    schema = selected_schema()
    assert 'UNATTENDED_SECONDARY_USER' not in schema.fields
    assert '--unattended-user-index' in generated_help(schema)
    result = resolve_config(schema, environ={'TUI_MODE': 'run-local'}, argv=['--unattended-user-index=1'])
    result.raise_for_errors()
    assert result.values['UNATTENDED_USER_INDEX'] == 1
    assert result.fields['UNATTENDED_USER_INDEX'].explicit
    assert resolve_config(schema, environ={'TUI_MODE': 'run-local'}).values.get('UNATTENDED_USER_INDEX') is None
    assert not schema.fields['UNATTENDED_USER_INDEX'].persist_default

@pytest.mark.parametrize('index', ['-1', 'invalid'])
def test_invalid_declared_index_is_rejected(index):
    assert resolve_config(selected_schema(), environ={'TUI_MODE': 'run-local'}, argv=['--unattended-user-index=' + index]).errors
