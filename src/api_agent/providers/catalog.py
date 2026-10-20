"""Bounded public catalog metadata; authentication never crosses this boundary."""
from dataclasses import asdict
import json
from .contracts import ContractError
from .llm import AvailableSelection

_FIELDS = {'selected_llm', 'description', 'features', 'display_name', 'version',
           'max_output_tokens', 'context_length', 'family'}


def catalog_to_wire(values):
    if type(values) is not tuple or len(values) > 256:
        raise ContractError('Invalid provider catalog')
    rows, seen = [], set()
    for value in values:
        if not isinstance(value, AvailableSelection):
            raise ContractError('Invalid provider catalog')
        row = asdict(value)
        row['features'] = list(row['features'])
        lifetimes = row.pop('cache_boundary_lifetimes')
        if lifetimes:
            row['cache_boundary_lifetimes'] = list(lifetimes)
        efforts = row.pop('reasoning_efforts')
        if efforts is not None:
            row['reasoning_efforts'] = list(efforts)
        checked = catalog_from_wire([row])[0]
        if checked.selected_llm in seen:
            raise ContractError('Duplicate provider catalog selection')
        seen.add(checked.selected_llm)
        rows.append(row)
    catalog_from_wire(rows)
    return rows


def catalog_from_wire(rows):
    if type(rows) is not list or len(rows) > 256:
        raise ContractError('Invalid provider catalog')
    values, seen = [], set()
    for row in rows:
        if type(row) is not dict or set(row) - {'reasoning_efforts', 'cache_boundary_lifetimes'} != _FIELDS:
            raise ContractError('Invalid provider catalog')
        for name, limit in (('selected_llm', 200), ('description', 512), ('display_name', 200), ('version', 100), ('family', 200)):
            text = row[name]
            if name in ('version', 'family') and text is None:
                continue
            if type(text) is not str or len(text) > limit or (text and not text.isprintable()):
                raise ContractError('Invalid provider catalog')
        if type(row['features']) is not list:
            raise ContractError('Invalid provider catalog')
        lifetimes = row.get('cache_boundary_lifetimes', [])
        if type(lifetimes) is not list:
            raise ContractError('Invalid provider catalog')
        efforts = row.get('reasoning_efforts')
        if efforts is not None and type(efforts) is not list:
            raise ContractError('Invalid provider catalog')
        try:
            value = AvailableSelection(**(row | {'features': tuple(row['features']), 'cache_boundary_lifetimes': tuple(lifetimes), 'reasoning_efforts': None if efforts is None else tuple(efforts)}))
        except (ValueError, TypeError):
            raise ContractError('Invalid provider catalog') from None
        if value.selected_llm in seen:
            raise ContractError('Duplicate provider catalog selection')
        seen.add(value.selected_llm)
        values.append(value)
    if len(json.dumps(rows, separators=(',', ':')).encode()) > 131072:
        raise ContractError('Provider catalog exceeds its metadata byte limit')
    return tuple(values)
