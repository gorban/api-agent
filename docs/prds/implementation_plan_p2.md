# Conditional preparation and host prerequisites

[Mise startup](implementation_plan_p.md) · [First launch](implementation_plan_p1.md)

## Surgical migration boundary

Keep the existing Python environment, requirement resolver, operation selection, TUI,
provider discovery and service lifecycles. Mise replaces manual executable setup;
it does not introduce a separate control environment, operation workers, environment
receipts, collection declarations, process fences or a new package-lock architecture.
Existing Python requirements remain authoritative and npm continues to use its lock.

## Downloadable tools

- [ ] Prepare Python and uv through the root mise inventory before entering the
  existing Python bootstrap. Preserve its existing base/development dependency
  preparation and reuse; do not claim it is a minimal TUI-only environment.
- [ ] Acquire Node/npm at the existing source-frontend and included GraphQL MCP setup boundaries, mkcert
  at the existing HTTPS boundary, and Java/Allure when the default report viewer
  is selected. Preserve explicit custom executable overrides.
- [ ] Keep Playwright package/driver/browser preparation with the existing invoking
  Python or npm owner. A browser binary and its host libraries are distinct.
- [ ] Reuse shared progress, deadlines, cancellation and cleanup interfaces.
  Propagate installation failures; do not silently use an incompatible tool.
- [ ] Do not acquire optional tools merely to open help/configuration.
  Preserve existing test-runner preparation policy; finer selected-test dependency
  minimization is not required for this launcher migration.

## Host capabilities

Docker is optional. Require an already installed, running and accessible supported
engine, client and the Compose capabilities actually used when selecting:

| Selected feature | Containers needed |
| --- | --- |
| Managed local Jaeger tracing | Jaeger and owned configuration/storage |
| Managed local LangFuse tracing | LangFuse and declared backing services |
| Managed LocalStack report storage | LocalStack and initialization |
| Containerized local API | API and selected dependency closure |
| Provider, workflow tool or test requiring containers | Its declared service graph |
| Container image build or container test | Applicable build/test capabilities |

- [ ] External tracing, external S3 and external API choices independently omit
  their corresponding local stacks. Disabling tracing requests no tracing backend;
  another selected feature may still need Docker. Keep existing explicit managed
  LocalStack selection rather than interpreting a loopback URL as permission.
- [ ] Manage selected application containers through existing owners, but never
  install, provision or start the user's container engine.
- [ ] Diagnose host browser libraries/fonts, graphical sessions, OpenSSL,
  certificate trust, helper authentication, credentials, registry permission and
  external access where consumed. Do not promise that mise and Docker supply every
  host capability or maintain distribution-specific privileged installers.
- [ ] Preserve actual browser conditions: RUN_UI=False omits the optional UI,
  not DEBUG viewing or provider consent that genuinely uses a browser. Direct
  SDK/key requests and reusable authentication need not launch a browser.
- [ ] Preserve existing recursive provider/tool/service discovery and inactive-owner
  exclusion. Do not add another traversal or implement a pack's unfinished services.

## Verification and acceptance

- [ ] Verify help, configuration, source/pack handoff, ordinary collection and unit
  execution remain available through the established bootstrap.
- [ ] Verify optional tool selection, warm reuse, unavailable downloads, custom
  overrides, cancellation and cleanup; cancellation must not abandon preparation.
- [ ] Exercise local/external/disabled service choices independently without changing
  their existing ownership contracts. Existing unit and synthetic service tests
  remain applicable; live account or unfinished E2E behavior is not newly claimed.
- [ ] Complete applicable unit gates and independent review. Retain fresh-machine,
  platform, real-engine/browser and human acceptance explicitly where unexecuted.
