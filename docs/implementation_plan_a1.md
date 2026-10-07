# Interactive .env configuration cleanup

## Checklist

- [x] List Setting, Effective value, and Source for the selected agent/resource-pack configuration registry, with a narrow unnamed first column showing a checkmark for deletable rows. Settings hidden from default review appear only while saved in a relevant `.env`. Exclude `UNATTENDED_AUTH`, managed in its dedicated mode. Do not require unrelated application/authentication settings to enter cleanup.
- [x] Omit the target-file section and target-switch shortcut. Use an agent/resource-pack `.env` Source to enable deletion; environment/CLI/default sources do not enable it.
- [x] Show contextual `d Delete .env assignment` and require literal `y` confirmation before deleting only the focused key, one setting at a time. Remove every copy from all relevant `.env` files, including duplicate assignments, while preserving unrelated content and writing each file atomically. Cancel changes nothing.
- [x] Refresh effective values and sources after deletion. Closing cleanup with `c` returns to default review when settings again need acknowledgement, before returning to the menu.
- [x] Keep cleanup interactive-only. Verify both-file deletion, confirmation cancellation, absence of batch marking, hidden-default cleanup (`UI_NX_APP`), preserved content, fallback values, excluded authentication JSON, and return to default review with temporary files.
