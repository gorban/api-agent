# Portable editable text selection

[Interactive launcher](implementation_plan_a.md)

- [x] Share text-selection bindings across configuration edits, authentication
  descriptors, private replies, confirmations and test search.
- [x] Support Option/Alt word movement and shifted word selection, Command/Super
  line movement and shifted selection, and Command/Super+A when delivered by the
  terminal. Preserve native Shift+Home/End and existing Control editing.
- [x] Advertise Ctrl+G Select all as a modifier-independent fallback for replacing
  a long effective value with typing, Delete/Backspace or paste. Selection alone
  must not change the value or persist configuration.
- [x] Preserve masked-field clipboard protections and private input logging.
  Verify real parser sequences and keyboard dispatch, replacement, inherited
  editor behavior and relevant coverage/reviews.
- [ ] Human verify macOS Terminal editing. A terminal-consumed or indistinguishable
  key combination cannot be recovered by application bindings; the explicit
  select-all fallback remains available.
