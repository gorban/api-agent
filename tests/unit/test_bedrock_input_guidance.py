"""Fixed native input failures survive ordinary setup serialization."""
import pytest

from api_agent.llm_providers.bedrock.login_transport import acquisition_error
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.setup_transport import _error, _raise_error


@pytest.mark.parametrize('reason,guidance', [
    ('input_closed', 'input channel closed'),
    ('input_limit', 'input exceeded the sign-in limit'),
    ('input_malformed', 'invalid input frame'),
    ('input_multiline', 'one complete line'),
    ('output_limit', 'output exceeded the sign-in limit'),
])
def test_fixed_local_failure_has_actionable_guidance_and_no_inference(reason, guidance):
    frame = dict(error='acquisition', stage='sign_in', kind='invalid_request', reason=reason)
    error = acquisition_error(frame)

    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(_error(error))

    assert caught.value.code == reason
    assert caught.value.kind == 'invalid_request'
    assert caught.value.execution == 'not_started'
    assert guidance in str(caught.value)


@pytest.mark.parametrize('reason', ['input_closed', 'input_limit', 'input_malformed', 'input_multiline', 'output_limit'])
def test_local_failure_cannot_claim_authentication_rejection(reason):
    frame = dict(error='acquisition', stage='sign_in', kind='authentication', reason=reason)
    with pytest.raises(ContractError, match='Invalid AWS sign-in failure response'):
        acquisition_error(frame)
