# Copy unattended-authentication descriptor fields

[Unattended authentication setup](implementation_plan_a2.md)

- [x] Expose contextual `^c Copy value` while a non-password descriptor field is focused in the account or password-manager editor, including vault, login/item name and manager CLI path. Copy highlighted input text, or its whole current value when no text is highlighted; an explicit empty value remains copyable.
- [x] Reuse the shared clipboard transport and generic success/failure feedback. Preserve read-only screen-selection precedence, field values, saved configuration and focus. Clear only the copied selection after success if its field, text and selection still match; retain it on failure or intervening edits.
- [x] Prevent masked inputs from exporting their content through either the contextual action or the focused widget's normal Ctrl+C fallback. Do not advertise a value-copy shortcut for a masked input.
- [x] Verify actual keyboard dispatch and footer availability, selected/full/empty values, read-only precedence, clipboard failure and asynchronous edit/focus changes. Complete applicable unit coverage and independent/external source review.
- [x] Human accepts account and manager field copying without another manual retest.
