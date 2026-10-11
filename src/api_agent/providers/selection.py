"""Provider-neutral catalog filtering, inference admission and default selection."""
import re

from .catalog import catalog_to_wire
from .contracts import ProviderAccessError


def compile_pattern(value):
    if type(value) is not str or not value or len(value) > 2048:
        raise ValueError('expected a nonempty regular expression of at most 2048 characters')
    try:
        return re.compile(value)
    except (re.error, OverflowError, RecursionError):
        raise ValueError('expected a valid regular expression') from None


class ModelSelectionPolicy:
    def __init__(self, config):
        import regex
        try:
            self.allowed = regex.compile(compile_pattern(config.get('LLM_ALLOWED_MODELS', '.*')).pattern, regex.VERSION0)
            self.default = (regex.compile(compile_pattern(config['LLM_DEFAULT_MODEL']).pattern, regex.VERSION0)
                            if 'LLM_DEFAULT_MODEL' in config else None)
        except (ValueError, OverflowError, RecursionError, regex.error):
            raise ProviderAccessError('invalid_request', 'Expected a valid, bounded model-selection regular expression.') from None

    @staticmethod
    def _matches(pattern, identifier):
        try:
            return pattern.fullmatch(identifier, timeout=0.02) is not None
        except TimeoutError:
            raise ProviderAccessError('invalid_request', 'Model selection regular expression exceeded its match budget.') from None

    def require_allowed(self, identifier):
        if (type(identifier) is not str or not identifier or len(identifier) > 200
                or not identifier.isprintable() or not self._matches(self.allowed, identifier)):
            raise ProviderAccessError('invalid_request', 'Selected model is not permitted by LLM_ALLOWED_MODELS.')

    def filter(self, selections):
        catalog_to_wire(selections)
        return tuple(value for value in selections if self._matches(self.allowed, value.selected_llm))

    def select_default(self, selections):
        from packaging.version import InvalidVersion, Version
        if self.default is None:
            raise ProviderAccessError('invalid_request', 'LLM_DEFAULT_MODEL is required to resolve a default model.')
        matches = [value for value in self.filter(selections)
                   if self._matches(self.default, value.selected_llm)]
        if not matches:
            raise ProviderAccessError('invalid_request', 'LLM_DEFAULT_MODEL matches no allowed model in the selected catalog.')
        if len(matches) == 1:
            return matches[0].selected_llm
        families = {value.family for value in matches}
        if None in families or len(families) != 1:
            raise ProviderAccessError('invalid_request', 'LLM_DEFAULT_MODEL must match one model family; providers must describe its family.')
        try:
            versions = [(Version(value.version), value.selected_llm) for value in matches
                        if value.version is not None]
        except InvalidVersion:
            raise ProviderAccessError('invalid_request', 'Matched models require comparable version metadata.') from None
        if len(versions) != len(matches):
            raise ProviderAccessError('invalid_request', 'Matched models require version metadata.')
        highest = max(version for version, identifier in versions)
        winners = [identifier for version, identifier in versions if version == highest]
        if len(winners) != 1:
            raise ProviderAccessError('invalid_request', 'LLM_DEFAULT_MODEL has multiple models at the highest version; narrow its pattern.')
        return winners[0]
