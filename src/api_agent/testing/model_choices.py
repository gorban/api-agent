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
