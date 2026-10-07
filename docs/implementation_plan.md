# api-agent checklist

## Checklist

- [ ] Build a Python TUI for starting the application and running unit, integration, and E2E tests. See [TUI grouping plan](implementation_plan_a.md).
- [ ] Start with a single LangGraph node instructed to say "Ok", exposed through the existing frontend API contract, with example integration and human-accepted E2E tests. See [initial LangGraph grouping plan](implementation_plan_e.md).
- [ ] Establish declarative formats for configuration, LangGraph flow, and other executable descriptions, parsed programmatically at runtime and used to derive configuration documentation and flow diagrams. See [declarative formats grouping plan](implementation_plan_b.md).
- [ ] Start a README describing application and TUI usage, with configuration documentation derived from executable descriptions. See [README grouping plan](implementation_plan_c.md).
- [ ] Establish Allure reporting for every test before writing the first test, with persistent results in S3 and an optional secondary results destination. See [Allure grouping plan](implementation_plan_d.md).
