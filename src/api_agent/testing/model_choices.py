"""Inert integration choices; capability evidence comes from the prepared catalog."""
from api_agent.providers.selection import ModelSelectionPolicy


def exact_model(config, key):
    """Require a provider-contributed exact identifier before provider setup."""
    model = config.get(key)
    if type(model) is not str or not model.strip() or model != model.strip():
        raise ValueError(f'{key} requires a qualified exact model ID')
    ModelSelectionPolicy(config).require_allowed(model)
    return model


def catalog_choice(catalog, model, efforts=(), *, require_metadata=True):
    """Do not infer capability or family/version from identifier spelling."""
    rows = [row for row in catalog if row.selected_llm == model]
    if len(rows) != 1:
        raise ValueError('Required exact model must be uniquely advertised: ' + model)
    row = rows[0]
    if require_metadata and (not row.family or not row.version):
        raise ValueError('Required model family/version is unavailable: ' + model)
    explicit = set(efforts) - {''}
    if explicit and ('reasoning_effort' not in row.features
                     or not explicit <= set(row.reasoning_efforts or ())):
        raise ValueError('Required reasoning effort is unavailable: ' + model)
    return row


def resolved_model(config, key, catalog):
    """Use an exact override, otherwise the application's normal default policy."""
    if config.get(key) not in (None, ''):
        return exact_model(config, key)
    return ModelSelectionPolicy(config).select_default(catalog)


def studio_choices(config, catalog):
    """Resolve missing selections without changing the provider or saved settings."""
    default = resolved_model(config, 'LLM_STUDIO_TEST_DEFAULT_MODEL', catalog)
    initial = catalog_choice(catalog, default, require_metadata=False)
    alternate = config.get('LLM_STUDIO_TEST_SELECTED_MODEL')
    if alternate == default:
        raise ValueError('Studio selection requires two distinct exact models')
    if alternate not in (None, ''):
        candidates = (catalog_choice(catalog, exact_model(config, 'LLM_STUDIO_TEST_SELECTED_MODEL'), require_metadata=False),)
    else:
        candidates = sorted((row for row in ModelSelectionPolicy(config).filter(catalog)
                             if row.selected_llm != default and row.family
                             and 'reasoning_effort' in row.features and row.reasoning_efforts),
                            key=lambda row: (row.family != initial.family, row.selected_llm))
    first = config.get('LLM_STUDIO_TEST_DEFAULT_EFFORT') or None
    second = config.get('LLM_STUDIO_TEST_SELECTED_EFFORT') or None
    controls = ('off', 'low', 'medium', 'high')
    if first and second and first == second:
        raise ValueError('Studio selection requires two distinct supported effort choices')
    if first:
        catalog_choice(catalog, default, (first,), require_metadata=False)
    if second and alternate not in (None, ''):
        catalog_choice(catalog, candidates[0].selected_llm, (second,), require_metadata=False)
    first_options = (first,) if first else tuple(e for e in controls if e in (initial.reasoning_efforts or ()))
    for other in candidates:
        second_options = (second,) if second else tuple(e for e in reversed(controls) if e in (other.reasoning_efforts or ()))
        for left in first_options:
            for right in second_options:
                if left != right:
                    catalog_choice(catalog, default, (left,), require_metadata=False)
                    catalog_choice(catalog, other.selected_llm, (right,), require_metadata=False)
                    return default, other.selected_llm, left, right
    return None


def reasoning_choices(config, catalog):
    """Derive only controls advertised by the selected model, never its spelling."""
    model = resolved_model(config, 'LLM_REASONING_TEST_MODEL', catalog)
    row = catalog_choice(catalog, model, require_metadata=False)
    supported = tuple(e for e in ('off', 'low', 'medium', 'high')
                      if 'reasoning_effort' in row.features and e in (row.reasoning_efforts or ()))
    if len(supported) < 2:
        return None
    low = config.get('LLM_REASONING_TEST_LOW_EFFORT') or ('low' if 'low' in supported and supported[-1] != 'low' else supported[0])
    high = config.get('LLM_REASONING_TEST_HIGH_EFFORT') or supported[-1]
    if ('off', 'low', 'medium', 'high').index(low) >= ('off', 'low', 'medium', 'high').index(high):
        raise ValueError('Reasoning contrast requires two distinct qualified explicit effort controls')
    catalog_choice(catalog, model, (low, high), require_metadata=False)
    return model, low, high


def probe_reasoning_tokens(provider, row, effort, *, timeout_seconds=150, max_output_tokens=None):
    """One accounted, bounded inference; report counters, never response content."""
    from uuid import uuid4
    from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
    from api_agent.providers.progress import report_timeout_sync
    if max_output_tokens is not None:
        if 'max_output_tokens' not in row.features:
            raise ValueError('Reasoning contrast output limit requires advertised model support')
        if row.max_output_tokens is not None and max_output_tokens > row.max_output_tokens:
            raise ValueError('Reasoning contrast output limit exceeds the advertised model limit')
    maximum = min(4096, row.max_output_tokens or 4096, max_output_tokens or 4096) if 'max_output_tokens' in row.features else None
    request = LLMRequest(row.selected_llm, CompletionRequest(
        (Message('user', (TextContent('What is 17 times 19? Reply with only the number.'),)),),
        generation=GenerationOptions(max_output_tokens=maximum, reasoning_effort=effort)), str(uuid4()))
    with report_timeout_sync('Reasoning-token reporting probe', min(150, timeout_seconds)):
        result = provider.complete(request)
    if result.outcome != 'completed':
        raise ValueError('Reasoning-token reporting probe did not complete')
    if result.actual_model not in (None, row.selected_llm):
        raise ValueError('Reasoning-token reporting probe returned a different model')
    count = result.usage.reasoning_tokens if result.usage is not None else None
    from api_agent.workflows.evidence import publish
    supported = type(count) is int and count >= 0
    publish({'kind': 'reasoning_token_probe', 'accepted': supported})
    return supported
