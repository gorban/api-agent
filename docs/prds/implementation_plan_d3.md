# Explicit Allure reporting opt-out

[Allure reporting](implementation_plan_d.md) · [Workflow source prerequisite](implementation_plan_b1c3.md)

This planned opt-out is the permissive escape hatch for testing uncommitted graph inputs. Normal integration/E2E execution retains Allure reporting and its committed-source prerequisite. It is not a separate workflow strictness setting.

## Configuration and visibility

- [ ] Add Boolean `SKIP_ALLURE_REPORTING`, default False, with `--skip-allure-reporting` and the same Boolean argument parsing and ordinary configuration precedence as DEBUG. Support explicit True/False CLI values and manually supplied `SKIP_ALLURE_REPORTING=True` in environment/dotenv configuration; False retains normal reporting and source checks.
- [ ] Mirror DEBUG's configuration-screen hiding, including Review configuration, Clear .env configuration and normal setting-edit/default suggestions. Do not encourage saving this as a default or silently write it to dotenv. Preserve normal validation of manually supplied values. Document the CLI option in `-h` / `--help`, explaining that it disables Allure and permits uncommitted workflow inputs.

## Runtime boundary

- [ ] With True, bypass Allure-only preparation, recording, attachments, finalized manifests and publication to primary, secondary or temporary Allure storage. Do not initialize reporting-only dependencies, services or clients for the skipped operation. Other application prerequisites remain effective. Never mark skipped publication as a publication pass; show a concise explicit reporting-skipped status in interactive and terminal summaries.
- [ ] Apply this consistently to interactive and NO_STD_IN integration/E2E, including DEBUG, failures and cancellation. Disable the current operation's `a View in Allure` actions and details links; do not accidentally open an older result as this run. Browsing already recorded history through the separate menu remains available.
- [ ] Leave execution acceptance, authentication, suite exclusion, timeout/cleanup, test outcomes, console/TUI diagnostics and independently configured local token CSV behavior intact. Preserve privacy even without reporting. This flag never authorizes inference, bypasses START/E2E acceptance or creates an unreported run in Allure-backed cost history. Current-run usage/estimates may still be displayed without persistent Allure ingestion.
- [ ] When False/omitted and graph documents are consumed, require the [committed document-hash pathway](implementation_plan_b1c3.md), including staged, unstaged, untracked and ignored-untracked inputs. Missing committed provenance fails before graph execution/inference with the commit-or-skip alternative. No warning-only bypass or independent strictness option is allowed. Unit/collection/static-view behavior stays outside test Allure recording as before.

## Acceptance

- [ ] Verify CLI Boolean forms, environment/dotenv precedence, default False, explicit False and malformed values through actual help and configuration screens. Prove the setting is hidden like DEBUG while manual configuration is honored.
- [ ] Exercise clean/dirty inputs with reporting enabled/skipped, interactive/NO_STD_IN/DEBUG and integration/E2E adapters, success/failure/interruption and returned selection/details. Assert no Allure writer, client/service initialization, attachment, local/secondary/S3 output or history ingestion on skip, while test results, usage, local CSV and suite locking remain correct. Verify historical menu access without false current-run links.
- [ ] Use mutations to detect ignored staged/untracked inputs, a warning-only source gate, reporting initialized despite skip and stale result navigation. Require applicable exact unit line/branch coverage, installed-package, independent review and public-boundary/index/metadata gates when implemented. No runtime change or human acceptance is claimed by this plan.
