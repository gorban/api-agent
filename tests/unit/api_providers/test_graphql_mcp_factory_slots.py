"""Selected GraphQL owners consume isolated local slots."""
import pytest

from api_agent.api_providers.graphql_mcp import GraphQLMcpError
from api_agent.api_providers.graphql_mcp.factory import create, create_tool


def test_named_owners_keep_configuration_separate(tmp_path):
    first = create_tool({'ENDPOINT': 'http://first.test/graphql',
                         'HEADERS': {'Authorization': 'Bearer first'},
                         'ROOT_FIELDS': ['first']}, {'owner_root': tmp_path})
    second = create_tool({'ENDPOINT': 'http://second.test/graphql',
                          'HEADERS': {'Authorization': 'Bearer second'},
                          'ROOT_FIELDS': ['second']}, {'owner_root': tmp_path})

    assert first is not second
    assert first._owner is not second._owner
    assert first._owner._endpoint == 'http://first.test/graphql'
    assert second._owner._endpoint == 'http://second.test/graphql'
    assert first._owner._headers == {'Authorization': 'Bearer first'}
    assert second._owner._headers == {'Authorization': 'Bearer second'}
    assert first._owner._root_fields == frozenset({'first'})
    assert second._owner._root_fields == frozenset({'second'})


@pytest.mark.parametrize('slot,legacy,local', [
    ('ENDPOINT', 'http://first.test/graphql', 'http://second.test/graphql'),
    ('HEADERS', {'X-Target': 'first'}, {'X-Target': 'second'}),
    ('ROOT_FIELDS', ['first'], ['second']),
])
def test_legacy_entry_rejects_ambiguous_local_slots(tmp_path, slot, legacy, local):
    config = {'API_ENDPOINT': 'http://first.test/graphql', 'API_' + slot: legacy,
              slot: local}

    with pytest.raises(GraphQLMcpError, match='tool_slots_conflict'):
        create(config, {'owner_root': tmp_path})


@pytest.mark.parametrize('slot', ['ENDPOINT', 'HEADERS', 'ROOT_FIELDS'])
def test_tool_entry_rejects_legacy_slots_before_acquisition(tmp_path, slot):
    config = {'ENDPOINT': 'http://first.test/graphql', 'API_' + slot: None}

    with pytest.raises(GraphQLMcpError, match='legacy_tool_slots_invalid'):
        create_tool(config, {'owner_root': tmp_path})


def test_legacy_entry_translates_all_slots_without_changing_input(tmp_path):
    config = {'API_ENDPOINT': 'http://first.test/graphql',
              'API_HEADERS': {'X-Target': 'first'}, 'API_ROOT_FIELDS': ['first']}

    owner = create(config, {'owner_root': tmp_path})

    assert owner._owner._endpoint == config['API_ENDPOINT']
    assert owner._owner._headers == config['API_HEADERS']
    assert owner._owner._root_fields == frozenset({'first'})
    assert set(config) == {'API_ENDPOINT', 'API_HEADERS', 'API_ROOT_FIELDS'}
