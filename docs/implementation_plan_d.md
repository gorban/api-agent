# Allure reporting and persistent test results


## Checklist

- [ ] Establish Allure output for every test before writing the first rewrite test, covering unit, integration, and human-accepted E2E runs through both the TUI and supported unattended CLI invocations.
- [ ] Persist all Allure results in S3 throughout development. Use LocalStack for local S3 testing.
- [ ] Add optional typed configuration for a secondary Allure results destination, such as a local directory. When configured, write results there as well as to S3; it must not replace primary S3 persistence.
- [ ] Provide the TUI's "Allure serve" mode for viewing test results.
