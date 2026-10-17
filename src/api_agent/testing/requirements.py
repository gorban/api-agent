"""One selected-test dependency plan for preflight and integration execution."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntegrationRequirementsPlan:
    requires_pack_api: bool
    requires_ui_session: bool = False
    requires_llm: bool = True

    @property
    def provider_kinds(self):
        kinds = {'llm'} if self.requires_llm else set()
        if self.requires_ui_session:
            kinds.add('ui')
        if self.requires_pack_api:
            kinds.update(('api', 'upstream', 'access'))
        return frozenset(kinds)

    def validate(self, context, config):
        if self.requires_pack_api:
            from api_agent.auth.extensions import validate_operation
            validate_operation(context, config)


def plan_integration_requirements(tests):
    """Keep legacy LLM intent; explicit UI-only tests do not consume an LLM.

    A UI session test that also consumes an LLM declares ``requires_llm``.
    Mixed selections union their requirements without changing operation mode.
    """
    tests = tuple(tests)
    return IntegrationRequirementsPlan(
        any('requires_pack_api' in test.tags for test in tests),
        any('requires_ui_session' in test.tags for test in tests),
        any('requires_ui_session' not in test.tags or 'requires_llm' in test.tags for test in tests))
