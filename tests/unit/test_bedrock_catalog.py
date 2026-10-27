import pytest
from api_agent.llm_providers.bedrock.catalog import MODELS, catalog, route, selection
from api_agent.providers.contracts import ContractError


def test_catalog_filters_exact_model_and_api_matrix():
    data = {'data': [{'id': model} for model in sorted(MODELS)] + [{'id': 'unknown'}]}
    assert tuple(row.selected_llm for row in catalog(data)) == tuple(sorted(MODELS))
    assert tuple(row.selected_llm for row in catalog(data, 'responses')) == ('openai.gpt-oss-120b', 'openai.gpt-oss-20b')
    assert route('zai.glm-5', 'completions') == '/v1'
    assert selection('zai.glm-5').context_length == 200000
    assert selection('zai.glm-4.7-flash').version == '4.7'
    assert selection('openai.gpt-oss-20b').max_output_tokens == 16000


@pytest.mark.parametrize('identifier,mode', [('unknown','completions'), ([], 'completions'), ('zai.glm-5','responses'), ('zai.glm-5','other')])
def test_selection_rejects_unqualified(identifier,mode):
    with pytest.raises(ContractError):
        selection(identifier,mode)


@pytest.mark.parametrize('raw', [None, {}, {'data': None}, {'data': [None]}, {'data': [{}]}, {'data': [{'id':''}]},
    {'data':[{'id':'a'*257}]}, {'data':[{'id':'same'},{'id':'same'}]}, {'data':[], 'has_more':True},
    {'data':[], 'next_page':'cursor'}, {'data':[], 'next_token':'cursor'}, {'data':[{}]*1025}])
def test_catalog_rejects_malformed_or_incomplete(raw):
    with pytest.raises(ContractError):
        catalog(raw)


def test_catalog_bad_mode():
    with pytest.raises(ContractError):
        catalog({'data':[]}, 'other')
