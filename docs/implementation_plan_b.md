# Declarative executable descriptions

## Checklist

- [ ] Define declarative formats for configuration and LangGraph flow, and identify other behavior that should use executable descriptions during planning.
  - [ ] Require a type for each configuration value and describe whether it is optional and whether it has a default. Use these declarations for the TUI/application type checks, missing-value handling, environment/CLI mapping, and generated configuration documentation.
- [ ] Parse these descriptions programmatically at runtime to configure the application and construct the executable LangGraph flow.
- [ ] Derive configuration documentation and flow diagrams from the same descriptions, avoiding separately maintained representations that can drift out of sync.
