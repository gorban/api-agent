# Standalone local trace viewer

[Tracing integration](implementation_plan_k1b2.md)

Future launcher migration: [shared viewer modes and configuration repair](implementation_plan_a13.md). Current standalone behavior remains the implemented contract until that task is delivered.

- [x] Provide `./launch.sh --trace-viewer` as a standalone headed browser for a conventional managed local collector. Disabled tracing and remote or custom destinations must fail clearly without starting services.
- [x] Resolve tracing configuration with normal precedence while validating only relevant fields; do not start application, UI, inference, provider authentication, or workflow/tool discovery. Help remains lazy.
- [x] Acquire the existing shared collector lease, open the existing authenticated dashboard in a managed inspection browser, and print the viewer URL and browser CDP endpoint. Retain the collector until command interruption, including after the managed browser closes, so copied URLs remain usable from other browsers.
- [x] On command interruption or failure, dispose browser and dashboard before releasing the collector lease. Preserve retryable cleanup ownership and report pending cleanup without repeated messages; never delete shared volumes or disrupt another consumer.
- [x] Stop owned collector services in reverse captured dependency order, skipping already-stopped IDs on retry and honoring other active consumer leases. Budget the entire multi-service stop, retain a safe chained underlying failure when cleanup remains pending, and never remove named volumes. Verify known trace readback after graceful stop and restart for both LangFuse and Jaeger.
- [x] Verify local/disabled/remote admission, configuration precedence, lazy help, cleanup ordering, failures, cancellation and retained ownership using offline fixtures and causal controls. Verify installed/source packaging and complete independent, full-suite and boundary reviews before publication.
- [x] After successful managed LangFuse dashboard login, print the configured dashboard username and password literally as indented lines beneath the viewer URL, solely in the command console. Keep API/exporter credentials and ordinary diagnostic/error output excluded. Print a clear Ctrl+C shutdown instruction; OTEL does not print dashboard credentials.
- [x] Human accepted the delivered standalone viewer and cleanup on October 30, 2026, including command-owned lifetime after browser closure and literal intended LangFuse dashboard login display.

Collector durability is limited to the exporter's acknowledged delivery and the managed backend's graceful stop/restart behavior; this does not promise per-trace filesystem synchronization. Named volumes and other consumers remain under the existing shared infrastructure ownership rules.

The original unit launcher passed 21,923 tests with 100% line and branch coverage. Both original headed viewer checks close cleanly; acknowledged synthetic traces remain readable after immediate graceful collector stop and restart. The three selected application/discovery/request integration tests pass with Allure publication and no cleanup error. Independent and external source reviews close both admission findings. Source and relocated wheel checks preserve the implementation and required notices.

A human-review follow-up changes browser-close behavior: the command must retain its collector until Ctrl+C, permitting inspection from another browser. The revised lifetime is verified for both backends: closing the managed browser leaves the command and HTTP dashboard available, and Ctrl+C completes owned cleanup. LangFuse prints its configured dashboard login literally; OTEL prints no login. The follow-up unit launcher passes 21,925 tests at 100% line and branch coverage.

Approval covers the delivered standalone feature. The planned menu-mode migration, pack-defined tracing providers and full GenAI/native input-output conformance retain their separate open requirements.
