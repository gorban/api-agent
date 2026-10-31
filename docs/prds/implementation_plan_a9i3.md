# Unit-test visible-window and CI audit

[Unit-suite performance](implementation_plan_a9i.md)

- [ ] Identify unit cases that create a visible browser, login dialog, native window or external application. Correlate owned process/window creation with exact test identities and the current DEBUG setting; distinguish necessary headful behavior from accidental defaults.
- [ ] Verify the normal suite on a host without a graphical desktop. Ordinary browser proofs should use headless execution; tests that specifically prove headful behavior need an isolated display or an explicitly separate platform gate with clear prerequisites. A developer DEBUG value must not expose unrelated unit-test windows.
- [ ] Preserve real browser, clipboard, display, cancellation and cleanup assertions where those are the behavior under test. Do not remove a required proof or hide failures to eliminate flashing windows. Specify unavoidable native-only checks separately and visibly.
- [ ] The Linux headed retained-browser and abrupt-exit proofs must run on a test-owned Xvfb display, independent of inherited X11 and Wayland settings. Xvfb and headed Playwright Chromium are prerequisites; failure to start the isolated display is a test failure. Other platforms require a separately verified isolation strategy before running these headed proofs.
- [ ] Ordinary unit tests must neutralize inherited Playwright inspector mode, which otherwise overrides explicit headless launches; tests of inspector policy may set it deliberately within their own scope.
- [x] Keep headed and headless managed Chromium usable when a long unit scratch path exceeds Chromium’s Unix singleton socket budget. Use a private short process-only temporary directory without moving the Playwright profile or changing global environment; retain it until browser disposal is confirmed. Verify real Chromium, failure and cancellation cleanup.
- [ ] Propose the smallest repair after profiling, verify no host-visible popups and no leaked processes, retain exact coverage and independent/external review, then obtain human acceptance.

The Linux headed proofs now use an isolated display and ordinary unit tests neutralize inherited inspector mode. The broader window audit remains open: the reported popup has not been correlated to a specific test, and isolation on macOS remains unverified.

Human testing after the display-isolation changes still observed two or three transient windows during a unit suite. Treat the visible-window requirement as unresolved: correlate new window/process owners with executing test identities before claiming the suite is free of host popups. Preserve the existing isolated-browser proofs and their assertions.
