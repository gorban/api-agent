# Interactive .env configuration cleanup

## Checklist

- [ ] Add a "Clear .env configuration" TUI mode using the shared declarative configuration registry. List every configuration variable applicable to the selected pack, its effective value, its value source, and whether it has an assignment in the active user's `.env` file. Show the file's resolved path. Do not require unrelated application/authentication settings to enter this mode.
- [ ] Indicate `.env` presence with a checkmark even if environment/CLI values currently override that assignment. Selecting a present entry changes its checkmark to `X` to mark deletion; allow selecting it again to undo the mark.
- [ ] When the focused entry exists in `.env`, show `d` to delete marked assignments from that file. Do not show or activate that hotkey on an entry supplied only by defaults/environment/CLI with no `.env` assignment. Such entries cannot be marked for deletion from `.env`.
- [ ] Remove all assignments for marked keys, including duplicate assignments, while preserving unrelated keys, comments, and formatting. Write the file atomically, refresh presence and effective-value/source displays, and show the revealed fallback from default/environment/CLI. Deleting `.env` entries must not unset environment variables or change CLI arguments.
- [ ] Keep this mode interactive-only and reject `NO_STD_IN=True` execution. Add deterministic tests with temporary `.env` files for marking/unmarking, duplicate removal, preserved content, overriding values, fallback resolution, and contextual `d` behavior.
