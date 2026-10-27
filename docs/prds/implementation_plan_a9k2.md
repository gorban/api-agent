# Private authentication clipboard input

[Interactive authentication](implementation_plan_a9k1.md)

## Requirements and acceptance

- [x] In masked authentication inputs, Ctrl+V reads the current operating-system clipboard on explicit user action. Never substitute Textual's internal clipboard, which can still contain previously copied output after the browser changes the OS clipboard.
- [x] Clear the app's internal clipboard when the authentication screen mounts and after copying its output, including failed copy attempts. Clear only local cached text: never send an empty native/terminal clipboard write or erase the user's system clipboard. Keep generic copy behavior and the native-reader no-fallback rule intact.
- [x] Use optional native text readers on supported platforms: pbpaste on macOS and wl-paste from wl-clipboard on Linux Wayland. Keep reads bounded to 65,536 UTF-8 bytes and a shared two-second component deadline. Reap owned subprocesses on failure, timeout and cancellation; keep values out of arguments, diagnostics and feedback.
- [x] If native reading is unavailable or fails, keep the input unchanged and give fixed guidance to use the terminal's Paste action, commonly Ctrl+Shift+V on Linux. Do not silently use an older value.
- [x] Preserve the complete payload of terminal Paste events in private inputs, including line breaks. Apply no generic password normalization; provider-owned code handling can normalize its own format. Ordinary inputs keep their existing behavior.
- [x] Keep the UI responsive during native reading. Reject late results and feedback after input/selection edits, focus changes, prompt transitions, modal transitions, cancellation, closure or unmount. Never copy a read secret into the app's shared clipboard buffer. Private Cut deletes the selection without copying it, and private Copy does not export input; ordinary input shortcuts remain unchanged.
- [x] Reproduce copying a selected login URL, external browser copying and then Ctrl+V with separate clipboard states. Exercise the shared interaction IPC and synthetic native worker; verify privacy, lifecycle, bounds, cleanup and meaningful causal controls with focused coverage and independent review.
- [x] Qualify the ordinary attended launcher flow with a real browser code. Synthetic input-origin tests do not establish successful account authentication or inference.

## Conditional setup

Install Linux Wayland's optional [wl-clipboard tools](https://github.com/bugaevc/wl-clipboard) in the environment running the launcher if native Ctrl+V is desired. The clipboard helper also needs access to the desktop session. macOS uses pbpaste when available. Other environments and unavailable helpers can use terminal Paste without installing a Python clipboard package. No clipboard is read merely by opening configuration or displaying a prompt.

On 2026-10-25 the user reported that Ctrl+V works and the existing attended Bedrock integration test passes through the ordinary launcher. This accepts the repaired direct sign-in/paste path. Other authentication modes and exact configuration/token-usage receipts remain separate.

## Output copy affordance

- [x] Use the standard highlighted ^c footer action for copying selected authentication output, with no dedicated copy button. Show the hint only while output text is selected, including selection changes without a focus change.
- [x] Keep priority Ctrl+C consumed even without selection; it must never become Quit or export masked input. Input selection must not reveal the output-copy hint, and completed/canceled/closed or suspended screens must not offer stale actions.
- [x] Focus-test hint visibility, output copying, secret-selection exclusion and no-selection Ctrl+C safety, including a causal control and independent review before human acceptance.

- [ ] Human acceptance of selection-only footer visibility and output-only copying through the ordinary authentication screen.
