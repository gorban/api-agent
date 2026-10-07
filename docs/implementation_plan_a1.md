# Interactive .env configuration cleanup

## Checklist

- [x] List Setting, Effective value, and Source for the selected agent/resource-pack configuration registry, including settings hidden from default review. Exclude `UNATTENDED_AUTH`, managed in its dedicated mode. Do not require unrelated application/authentication settings to enter cleanup.
- [x] Omit the presence column, target-file section, and target-switch shortcut. Use an agent/resource-pack `.env` Source to enable deletion; environment/CLI/default sources do not enable it. Enter marks/unmarks eligible rows with `X` before the setting name.
- [x] Show contextual `d` and require literal `y` confirmation before deleting marked keys, or the focused key if none are marked. Remove every copy from all relevant `.env` files, including duplicate assignments, while preserving unrelated content and writing each file atomically. Cancel changes nothing.
- [x] Refresh effective values and sources after deletion. Closing cleanup with `c` returns to default review when settings again need acknowledgement, before returning to the menu.
- [x] Keep cleanup interactive-only. Verify both-file deletion, confirmation cancellation, hidden-default cleanup (`UI_NX_APP`), preserved content, fallback values, excluded authentication JSON, and return to default review with temporary files.
