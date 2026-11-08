"""Validate display projections supplied explicitly by an admitted application."""
from copy import deepcopy
import json

from api_agent.providers.contracts import ContractError


def project_application_result(value, projector):
    """Keep standard identity/accounting intact while permitting JSON display fields."""
    if projector is None:
        return value
    try:
        protected = {key: item for key, item in value.items() if key != 'response_text'}
        expected = json.dumps(protected, sort_keys=True, allow_nan=False)
        projected = projector(deepcopy(value))
        if type(projected) is not dict or type(projected.get('response_text')) is not str:
            raise ValueError('Invalid display envelope')
        actual = {key: projected[key] for key in protected}
        if json.dumps(actual, sort_keys=True, allow_nan=False) != expected:
            raise ValueError('Changed result metadata')
        encoded = json.dumps(projected, sort_keys=True, allow_nan=False)
        if len(encoded.encode('utf-8')) > 1048576 or json.loads(encoded) != projected:
            raise ValueError('Invalid JSON display envelope')
    except Exception:
        raise ContractError('Application result projection failed.') from None
    return projected
