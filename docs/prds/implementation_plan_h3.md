# Shared dotenv configuration layers

Resource packs may declare optional shared configuration files using `[configuration] dotenv = ["../common/.env"]`. The table accepts only `dotenv`; its value must be an array of nonblank relative paths. Resolve entries against the pack root and deduplicate in declaration order. Paths may address sibling packs. Missing files are optional; manifests carry paths rather than configuration contents.

## Requirements and acceptance

- [x] Read base `.env`, declared shared files, launcher `.env`, and selected pack `.env` in order, with stable path deduplication. Preserve environment, CLI, and interactive precedence.
- [x] Apply the same layers before provider selection in bootstrap and ConfigSession.
- [x] Include shared files in normal Switch target, save, Clear, cleanup, and source provenance behavior. Saving uses the existing transactional migration to the chosen target; Clear removes persisted selections through the normal editor.
- [x] Reject malformed declarations and absolute or blank paths; allow empty tables and lists and absent optional files.
- [x] Verify with synthetic sibling packs and values, including provider selection, ordering, persistence, and existing runtime compatibility.
- [x] Support quoted literal dotenv assignments followed by whitespace and an inline comment. Preserve hashes and escape sequences within the literal, generated escaped markers, and disabled shell expansion; reject unmatched quotes and malformed suffixes.
- [x] Independent review of implementation and rejecting controls.
- [ ] Human acceptance of shared configuration editing.
