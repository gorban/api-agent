"""Choices validate locally and catalog capability checks fail closed."""
from types import SimpleNamespace
import pytest
from api_agent.testing.model_choices import exact_model, catalog_choice
from api_agent.providers.contracts import ProviderAccessError


@pytest.mark.parametrize('value', [None, '', ' ', 1, ' model', 'model '])
def test_exact_model_requires_nonempty_unpadded_identifier(value):
    with pytest.raises(ValueError, match='CHOICE'):
        exact_model({'CHOICE':value}, 'CHOICE')


def test_exact_model_respects_policy_without_interpreting_id_as_regex():
    assert exact_model({'CHOICE':'vendor.model:1'}, 'CHOICE') == 'vendor.model:1'
    with pytest.raises(ProviderAccessError):
        exact_model({'CHOICE':'vendor.model:1','LLM_ALLOWED_MODELS':'other'}, 'CHOICE')


def row(**changes):
    return SimpleNamespace(**(dict(selected_llm='model', family='family',version='1',features=('reasoning_effort',),reasoning_efforts=('low','high')) | changes))


@pytest.mark.parametrize('rows', [[], [row(),row()]])
def test_catalog_requires_unique_exact_id(rows):
    with pytest.raises(ValueError, match='uniquely'):
        catalog_choice(rows, 'model')


@pytest.mark.parametrize('changes', [{'family':None}, {'version':''}])
def test_catalog_requires_provider_comparison_metadata(changes):
    with pytest.raises(ValueError, match='family/version'):
        catalog_choice([row(**changes)], 'model')


@pytest.mark.parametrize('efforts', [None, ('low',)])
def test_catalog_requires_all_explicit_efforts(efforts):
    with pytest.raises(ValueError, match='effort'):
        catalog_choice([row(reasoning_efforts=efforts)], 'model', ('low','high'))


@pytest.mark.parametrize('efforts', [(), ('',), ('low','high')])
def test_catalog_preserves_advertised_metadata(efforts):
    selected = row()
    assert catalog_choice([row(selected_llm='other'), selected], 'model', efforts) is selected


def test_non_ui_choice_does_not_require_comparison_metadata():
    selected = row(family=None, version=None)
    assert catalog_choice([selected], 'model', ('low','high'), require_metadata=False) is selected


def test_bedrock_explicit_choice_does_not_silently_change_route_qualification():
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.testing.reasoning_contrast import scope
    root = Path(__file__).resolve().parents[2]
    values = {'LLM_PROVIDER':str(root/'src/api_agent/llm_providers/bedrock/factory.py')+':factory',
        'BEDROCK_API_MODE':'completions', 'LLM_REASONING_TEST_MODEL':'openai.gpt-oss-120b', 'TUI_MODE':'integration', 'ALLURE_RESULTS_DIR':'', 'SKIP_ALLURE_REPORTING':'True'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=root)
    result = resolve_config(schema, environ={}, dotenv={}, overrides=values)
    assert result.values['LLM_REASONING_TEST_MODEL'] == 'openai.gpt-oss-120b'
    with pytest.raises(ValueError, match='reasoning-token reporting'):
        scope(result.values)
    assert result.values['BEDROCK_API_MODE'] == 'completions'



def test_explicit_effort_requires_feature_even_when_catalog_lists_efforts():
    from api_agent.providers.llm import AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
    selected = AvailableSelection('model', features=(), reasoning_efforts=('low','high'))
    request = LLMRequest('model', CompletionRequest((Message('user',(TextContent('Hello'),)),),
        generation=GenerationOptions(reasoning_effort='high')))
    from api_agent.providers.control import OperationalError
    with pytest.raises(OperationalError, match='unsupported'):
        selected.validate(request)
    with pytest.raises(ValueError, match='Required reasoning effort'):
        catalog_choice([selected], 'model', ('low','high'), require_metadata=False)


@pytest.mark.parametrize('efforts', [(), ('',)])
def test_omitted_effort_does_not_require_advertised_feature(efforts):
    from api_agent.providers.llm import AvailableSelection
    selected = AvailableSelection('model', features=(), reasoning_efforts=('low','high'))
    assert catalog_choice([selected], 'model', efforts, require_metadata=False) is selected


def test_real_bedrock_oss_catalog_advertises_feature_and_requested_efforts():
    from api_agent.llm_providers.bedrock.catalog import selection
    selected = selection('openai.gpt-oss-20b', 'responses')
    assert catalog_choice([selected], selected.selected_llm, ('low','high'), require_metadata=False) is selected


def test_gpt_profile_preserves_unset_cap_and_documented_defaults():
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root=Path(__file__).resolve().parents[2]
    values={'LLM_PROVIDER':str(root/'src/api_agent/llm_providers/gpt/factory.py')+':factory',
        'TUI_MODE':'integration','ALLURE_RESULTS_DIR':'','SKIP_ALLURE_REPORTING':'True'}
    schema=bootstrap_schema(load_shared_schema(),['--help'],{},{},values,owner_root=root)
    result=resolve_config(schema,environ={},dotenv={},overrides=values)
    assert not result.errors
    assert result.values.get('LLM_REASONING_TEST_MAX_OUTPUT_TOKENS') is None
    assert result.values['LLM_REASONING_TEST_MODEL']=='gpt-6-luna'
    assert result.values['LLM_REASONING_TEST_TIMEOUT_SECONDS'] == 150
    assert result.values['LLM_SETUP_REQUEST_TIMEOUT_SECONDS'] == 300
    assert result.values['LLM_REASONING_TEST_HIGH_EFFORT']=='high'
    assert result.values['LLM_STUDIO_TEST_SELECTED_EFFORT']=='off'


@pytest.mark.parametrize('base', ['https://openrouter.ai/api/v1', 'https://openrouter.ai/api/v1/'])
def test_gpt_openrouter_defaults_select_verified_route_models(base):
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.llm_providers.gpt.reasoning import reasoning_efforts
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    values = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory',
              'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': base, 'GPT_API_MODE': 'responses',
              'TUI_MODE': 'integration', 'ALLURE_RESULTS_DIR': '', 'SKIP_ALLURE_REPORTING': 'True'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=root)
    result = resolve_config(schema, environ={}, dotenv={}, overrides=values)
    assert not result.errors
    assert result.values['LLM_WORKFLOW_TEST_MODEL'] == 'openai/gpt-6-luna'
    assert result.values['LLM_REASONING_TEST_MODEL'] == 'openai/gpt-6-luna'
    assert result.values['LLM_STUDIO_TEST_DEFAULT_MODEL'] == 'openai/gpt-6-sol'
    assert result.values['LLM_STUDIO_TEST_SELECTED_MODEL'] == 'openai/gpt-6-luna'
    assert result.values['GPT_API_MODE'] == 'responses'
    assert result.values['LLM_REASONING_TEST_MAX_OUTPUT_TOKENS'] == 16384
    for model, efforts in [('openai/gpt-6-luna', ('low', 'high', 'off')),
                           ('openai/gpt-6-sol', ('medium',))]:
        assert set(efforts) <= set(reasoning_efforts(model, base_url='https://openrouter.ai/api/v1'))


@pytest.mark.parametrize('auth,base', [('subscription', 'https://openrouter.ai/api/v1'),
                                      ('api_key', 'https://compatible.example/v1')])
def test_gpt_other_routes_retain_original_defaults(auth, base):
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    values = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory',
              'GPT_AUTH_MODE': auth, 'GPT_BASE_URL': base, 'TUI_MODE': 'integration',
              'ALLURE_RESULTS_DIR': '', 'SKIP_ALLURE_REPORTING': 'True'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=root)
    result = resolve_config(schema, environ={}, dotenv={}, overrides=values)
    assert result.values['LLM_WORKFLOW_TEST_MODEL'] == 'gpt-6-luna'
    assert result.values['LLM_STUDIO_TEST_SELECTED_MODEL'] == 'gpt-5.6-luna'
    assert result.values.get('LLM_REASONING_TEST_MAX_OUTPUT_TOKENS') is None


def test_gpt_openrouter_explicit_test_models_override_route_defaults():
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    values = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory',
              'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': 'https://openrouter.ai/api/v1',
              'TUI_MODE': 'integration', 'ALLURE_RESULTS_DIR': '', 'SKIP_ALLURE_REPORTING': 'True'}
    explicit = {'LLM_WORKFLOW_TEST_MODEL': 'custom/workflow', 'LLM_REASONING_TEST_MODEL': 'custom/reasoning',
                'LLM_STUDIO_TEST_DEFAULT_MODEL': 'custom/initial', 'LLM_STUDIO_TEST_SELECTED_MODEL': 'custom/alternate',
                'LLM_REASONING_TEST_MAX_OUTPUT_TOKENS': '4096'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=root)
    result = resolve_config(schema, environ={}, dotenv=explicit, overrides=values)
    assert {key: result.values[key] for key in explicit} == explicit | {'LLM_REASONING_TEST_MAX_OUTPUT_TOKENS': 4096}


def test_gpt_openrouter_explicit_empty_limit_keeps_application_default():
    from pathlib import Path
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    values = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory',
              'GPT_AUTH_MODE': 'api_key', 'GPT_BASE_URL': 'https://openrouter.ai/api/v1',
              'TUI_MODE': 'integration', 'ALLURE_RESULTS_DIR': '', 'SKIP_ALLURE_REPORTING': 'True'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, values, owner_root=root)
    result = resolve_config(schema, environ={}, dotenv={'LLM_REASONING_TEST_MAX_OUTPUT_TOKENS': ''},
                            overrides=values)
    assert result.values['LLM_REASONING_TEST_MAX_OUTPUT_TOKENS'] is None
