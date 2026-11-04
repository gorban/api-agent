# Terminal Allure history viewer

[Allure requirements](implementation_plan_d.md)

- [x] Support `--no-std-in --tui-mode allure` without reading stdin or requiring a TUI. Share the interactive history source, viewer, managed browser, port, CLI, history window and reporting service ownership rather than starting a second server implementation.
- [x] Respect primary/secondary history and managed-local storage preparation. Keep owned infrastructure, report server and browser alive until Ctrl+C, then dispose the viewer and release reporting leases in the same event loop, including startup failure and interruption.
- [x] Keep disposal failures visible, retain retry capabilities and avoid releasing successfully closed owners again. Scrub startup and cleanup diagnostics; do not print raw browser errors or configured credentials.
- [x] Complete applicable deterministic coverage, rejection mutations and independent review before human acceptance.
- [ ] Human acceptance of terminal history viewing, browser lifetime and Ctrl+C disposal with configured storage.
