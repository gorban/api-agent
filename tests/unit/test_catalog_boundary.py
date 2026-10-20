"""Catalog metadata is bounded, credential-free and validated on both sides."""
from dataclasses import asdict

import pytest

from api_agent.providers.catalog import catalog_from_wire, catalog_to_wire
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import AvailableSelection
from api_agent.providers.setup_transport import _error, _raise_error


def row():
    return asdict(AvailableSelection('gpt-example', display_name='Example', version='v1', max_output_tokens=2048, context_length=8192)) | {'features': [], 'cache_boundary_lifetimes': []}


def test_full_and_unknown_metadata_round_trip():
    values = (AvailableSelection('one'), AvailableSelection('two', 'Description', ('tools',), 'Two', 'v2', 100, 500, family='example'))
    assert catalog_from_wire(catalog_to_wire(values)) == values
    assert catalog_from_wire([]) == () and catalog_to_wire(()) == []


@pytest.mark.parametrize('value', [None, [], {}, tuple(range(257)), (None,)])
def test_invalid_provider_result_rejected(value):
    with pytest.raises(ContractError):
        catalog_to_wire(value)


@pytest.mark.parametrize('value', [None, (), {}, [None], [{}], [row() | {'extra': True}], [row()] * 257])
def test_invalid_wire_shape_rejected(value):
    with pytest.raises(ContractError):
        catalog_from_wire(value)


@pytest.mark.parametrize('field,value', [('selected_llm', ''), ('selected_llm', 0), ('selected_llm', 'x'*201), ('selected_llm', '\n'),
    ('description', 'x'*513), ('description', False), ('description', '\n'), ('display_name', 'x'*201), ('display_name', None),
    ('version', 'x'*101), ('version', 2), ('version', '\n'), ('features', ()), ('features', ['unsupported']), ('features', ['tools','tools']),
    ('max_output_tokens', True), ('max_output_tokens', 0), ('max_output_tokens', -1), ('max_output_tokens', 2147483648),
    ('context_length', '100'), ('context_length', 0), ('family', ''), ('family', False),
    ('family', 'x'*201), ('family', '\n')])
def test_invalid_wire_value_rejected(field, value):
    with pytest.raises(ContractError):
        catalog_from_wire([row() | {field: value}])


def test_duplicates_rejected_on_each_boundary():
    for operation, value in [(catalog_from_wire, [row(), row()]), (catalog_to_wire, (AvailableSelection('same'), AvailableSelection('same')))]:
        with pytest.raises(ContractError, match='Duplicate'):
            operation(value)


@pytest.mark.parametrize('updates', [{'status': True}, {'status': 600}, {'message': None}, {'message': ''}, {'code': '\n'}, {'type': 2}, {'request_id': 'x'*101}, {'kind': 'bad'}])
def test_invalid_safe_error_fields_rejected(updates):
    with pytest.raises(ValueError):
        ProviderAccessError(**({'kind': 'authentication', 'message': 'Rejected'} | updates))


def test_provider_error_round_trip_preserves_safe_detail_and_redacts_capability():
    capability = 'synthetic-capability-value'
    original = ProviderAccessError('authorization', 'Permission denied: ' + capability, status=403, code='denied', type='permissions', request_id=capability)
    frame = _error(original, capability)
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(frame)
    assert caught.value.status == 403 and caught.value.code == 'denied' and caught.value.type == 'permissions'
    assert caught.value.request_id == '[redacted]' and str(caught.value).splitlines()[0] == 'Permission denied: [redacted]'
    assert 'HTTP status: 403' in str(caught.value) and 'request ID: [redacted]' in str(caught.value)
    with pytest.raises(ProviderAccessError, match='Permission'):
        _raise_error(_error(original))


@pytest.mark.parametrize('update', [{'extra': 1}, {'index_source': 'dotenv'}, {'details': []}, {'details': {}},
    {'details': {'kind': 'bad', 'status': None, 'code': None, 'type': None, 'request_id': None}}, {'message': ''}])
def test_malformed_provider_error_frame_is_not_trusted(update):
    frame = _error(ProviderAccessError('authentication', 'Rejected')) | update
    with pytest.raises(ContractError):
        _raise_error(frame)


@pytest.mark.parametrize('character,count', [('x', 256), ('界', 90)])
def test_catalog_byte_limit_is_same_for_local_and_wire(character, count):
    values = tuple(AvailableSelection(f'gpt-{index}', character*512, (), character*200, character*100) for index in range(count))
    rows = [asdict(value) | {'features': [], 'cache_boundary_lifetimes': []} for value in values]
    for operation, data in [(catalog_to_wire, values), (catalog_from_wire, rows)]:
        with pytest.raises(ContractError, match='byte limit'):
            operation(data)
