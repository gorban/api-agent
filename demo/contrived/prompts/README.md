# Contrived prompt maintenance

[produce.md](produce.md) defines the executable producer instructions for both components. Model output controls routing/readiness; guards and format checks remain deterministic.

Before changing these instructions, review the [calibration refresh procedure](../../../docs/workflow_calibration.md) and the [mandatory coverage requirements](../../../docs/prds/implementation_plan_b2b1.md). A changed prompt or model needs reviewed model-specific bounds; normal integration coverage never invokes calibration or uses another model's budget.
