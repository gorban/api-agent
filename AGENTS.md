## **RULES**

1. Any proposed change to this AGENTS.md is discussion only until the user explicitly approves the exact edit.

2. Review proposed rules one line at a time, offering up to three alternatives, including omission.

3. Ground each rule in a specific agent mistake that clearer code or architecture would not resolve. Good: run available local CI checks, such as unit tests and static analysis, before pushing changes. Bad: generate repository-navigation instructions from `/init` instead of fixing an unclear structure.

4. Use focused mutation testing when multiple checks could reject a test input: temporarily disable the intended check and verify that its rejection test fails.
   - Good: test an expired JWT with a valid signature; disable only expiry validation, confirm the test fails because authentication succeeds, then restore validation.
   - Bad: test an expired JWT with an invalid signature; the test still passes with expiry validation disabled, proving only signature rejection.

5. Before committing code changes, require all applicable unit tests to pass with 100% line and branch coverage.

6. When inspecting a browser started by launch.py, attach to its printed loopback CDP endpoint and use the existing context/page. Playwright MCP’s extension connection page is a separate browser; do not inspect it as the application session. launch.py owns cleanup.
