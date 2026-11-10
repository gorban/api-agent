"""One selected-test dependency plan for preflight and integration execution."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntegrationRequirementsPlan:
    requires_pack_api: bool
    requires_ui_session: bool = False
    requires_llm: bool = True
    requires_owned_ui_session: bool = False

    @property
    def workspace_kinds(self):
        """Provider kinds whose declared workspaces must be prepared before workers start.

        Owned-UI tests start their own provider, so they never join ``provider_kinds``,
        yet they still consume the same installed workspace.
        """
        return frozenset({'ui'} if self.requires_ui_session or self.requires_owned_ui_session else ())

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
    An owned-UI E2E test prepares its own browser and provider inside the test.
    Mixed selections union their requirements without changing operation mode.
    """
    tests = tuple(tests)
    return IntegrationRequirementsPlan(
        any('requires_pack_api' in test.tags for test in tests),
        any('requires_ui_session' in test.tags for test in tests),
        any((not ({'requires_ui_session', 'requires_owned_ui_session'} & set(test.tags)))
            or 'requires_llm' in test.tags for test in tests),
        any('requires_owned_ui_session' in test.tags for test in tests))
