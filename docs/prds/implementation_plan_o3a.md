# Demo API landing page

[Verified API demonstration](implementation_plan_o3.md)

- [x] Serve short friendly HTML at exact GET / and /graphql, with a clickable GraphQL path and links to synthetic data and readiness. Explain POST, application/json, a complete sample request and the existing read-only/query-size/body-size/variables/operation-name limits.
- [x] Keep GET informational: never execute a query from URL parameters. Preserve readiness identity JSON, existing POST validation, synthetic-only data, bounded request handling and JSON errors on unknown routes.
- [x] Close HTTP regressions, meaningful routing mutation, applicable coverage, installed/package and independent/external review.
- [ ] Human open the service home and follow its endpoint link; verify that the guide describes how to call the API.

**Deferred human review**

The landing page and required verification/reviews are complete. Only explicit
human acceptance of this guide remains unchecked above.
