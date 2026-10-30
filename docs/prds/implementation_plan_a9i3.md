# Unit-test visible-window and CI audit

[Unit-suite performance](implementation_plan_a9i.md)

- [ ] Identify unit cases that create a visible browser, login dialog, native window or external application. Correlate owned process/window creation with exact test identities and the current DEBUG setting; distinguish necessary headful behavior from accidental defaults.
- [ ] Verify the normal suite on a host without a graphical desktop. Ordinary browser proofs should use headless execution; tests that specifically prove headful behavior need an isolated display or an explicitly separate platform gate with clear prerequisites. A developer DEBUG value must not expose unrelated unit-test windows.
- [ ] Preserve real browser, clipboard, display, cancellation and cleanup assertions where those are the behavior under test. Do not remove a required proof or hide failures to eliminate flashing windows. Specify unavoidable native-only checks separately and visibly.
- [ ] Propose the smallest repair after profiling, verify no host-visible popups and no leaked processes, retain exact coverage and independent/external review, then obtain human acceptance.

This future investigation does not claim the current suite already works without a desktop or that the reported popups have a confirmed source.
