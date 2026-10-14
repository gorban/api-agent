"""One selected-test dependency plan for preflight and integration execution."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntegrationRequirementsPlan:
    requires_pack_api: bool

    def validate(self, context, config):
        if self.requires_pack_api:
            from api_agent.auth.extensions import validate_operation
            validate_operation(context, config)


def plan_integration_requirements(tests):
    """API integrations declare their pack identity/API dependency explicitly.

    Base/provider-only tests require no pack API. The same immutable marker
    survives collection, selection and execution verification.
    """
    tests = tuple(tests)
    return IntegrationRequirementsPlan(any('requires_pack_api' in test.tags for test in tests))
