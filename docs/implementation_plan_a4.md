# Test authentication and Playwright failure evidence

## Checklist

- [ ] Implement unattended LLM provider authentication preflight for integration and E2E execution using the [role arrays, index selection, and credential helpers](implementation_plan_a2.md), when required by the selected provider. Select the LLM-user entry before starting login; an empty LLM-user array uses attended login instead. Do not run that preflight for unit tests, collect-only, help, or local startup. LLM provider authentication hooks also support separate [on-demand local API authentication](implementation_plan_a5.md). Missing/cleared profiles retain attended authentication. Automatic login does not bypass the interactive test-selection Continue action or E2E human acceptance.
- [ ] Before implementing the live login flow, launch a Playwright MCP session to give the implementing agent browser control, then walk through the actual authentication pages step by step with the user. Observe locators, redirects, popup/tab behavior, and account-dependent choices before coding them. Document the observed branches and completion checks in the implementation task; do not assume one fixed page sequence from the replaced app.
  - [ ] Exercise the required "LLM User" flow when the selected provider requires it. Resource packs select upstream API authentication extensions and their configuration. Keep the pack's API test identity separate from the LLM provider session. Pass the declared authentication context to the selected API extension so it can validate identity relationships before upstream requests, without encoding provider-specific checks in shared code.
  - [ ] Observe both a first login and subsequent login when available. A passkey-first screen may require selecting other sign-in options and then authenticator code; a subsequent visit may offer authenticator code directly. Support either observed route without requiring the same intermediate screen every time.
- [ ] Implement visible-state-driven conditional login steps with bounded waits. Retrieve the password only when its entry is required, and a fresh TOTP when an authenticator challenge is shown. Follow alternate-method controls to switch from passkey to authenticator when required; do not stall waiting for a mandatory passkey interaction.
  - [ ] Support password-only accounts. If no passkey or authenticator challenge is visible, wait briefly for a verified authenticated redirect/session/application state rather than treating absence of a challenge as proof of success. Fail on the bounded authentication timeout or an explicit login error, with failure evidence in Allure.
  - [ ] Keep password-manager CLI retrieval noninteractive and preserve its immediate stdin/login-required failure behavior. Do not log retrieved passwords/TOTP values or persist them in test artifacts. Support cancellation through the shared test runner rather than waiting indefinitely on a login page.
- [ ] Add a shared Playwright error-evidence handler for any Playwright exception, including timeout, navigation/locator/browser errors, and authentication failure, across integration authentication and E2E test execution.
  - [ ] Before response-specific screenshot logic, navigation, teardown, or closing browsers, capture the current whole browser viewport for every open tab/page in every test-owned browser context. Do not require an agent response to exist or maximize/scroll first: error screenshots preserve whatever state is currently visible, including login pages and popups. Capture additional full-page evidence where available without losing that first current-state image.
  - [ ] Iterate all open tabs rather than only the active page, and attach each image with its tab/context identity and URL to the current Allure test/attempt or suite-auth setup failure. Capture best-effort when a tab is already closed or the browser disconnected; record unavailable evidence without hiding the original exception or stopping capture of other accessible tabs.
  - [ ] Persist these attachments through normal Allure/S3 history, including failures before a test body or response exists. Ensure a suite-auth setup failure is a visible failed Allure result, not an orphan screenshot. Normal successful response screenshots still maximize the modal and scroll to the top under the [E2E evidence rules](implementation_plan_a3.md).
- [ ] Add controlled-page and simulated-CLI tests for password-only login, passkey-to-authenticator switching, direct authenticator challenge, verified completion versus timeout, pack API test identity/LLM session isolation, and all-tab failure capture. Do live login verification only after the MCP walkthrough and normal interactive test acceptance; no live LLM is needed to validate login state handling.

## Shared implementation destinations

```text
src/api_agent/
├── auth/browser_login.py          # Provider-neutral browser-login test support
└── testing/browser_evidence.py    # All-tab current-state failure attachments
```

Resource packs select their test authentication adapters; they do not copy the credential retrieval or browser-evidence implementation.

## Playwright application dependency and setup

- [x] Declare Python Playwright in the shared development extra, separate from mandatory runtime dependencies. The local development virtual environments have Playwright 1.63.0 installed; matching Chromium launched headlessly and passed a local DOM smoke assertion. This verifies browser execution, not authentication.
- [ ] Use the same lazily imported Python Playwright login primitives in test preflight and agent-requested LLM provider authentication. When that optional runtime feature is enabled, its environment/image must include Playwright and the matching browser; otherwise fail with an actionable dependency error or retain the attended public auth challenge. Do not require browser dependencies for unit/help/collection or attended-only operation.
- [ ] Integrate optional browser dependency readiness with the launch/setup path; download version-matched Chromium explicitly during browser setup, not on every launch or API request. Support headless default and explicit headed auth debugging. Include bounded startup/cancellation and a deterministic missing-package/browser check.
- [ ] Document application/test browser setup for macOS in README: activate the repo-local .venv, install the shared dev extra, and run `python -m playwright install chromium`. Keep workstation Fedora/Distrobox details out of those README instructions.
