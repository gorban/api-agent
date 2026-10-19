# Family defaults and reasoning selection

[Reasoning and selection identity](implementation_plan_f2f.md)

- [ ] Advertise optional application-catalog `family_defaults` metadata mapping each resolvable family to one exact advertised model ID. Preserve the configured overall default within its family; otherwise choose the unique newest comparable advertised version using the existing version-ordering contract. A sole row is unambiguous. Multiple rows with missing, opaque or tied highest versions require an explicit selection. Catalog order, labels and model-ID parsing never decide a winner.
- [ ] Studio resolves an unversioned family through that metadata, preserving compatibility with older catalogs whose overall default belongs to the family. Pin the resolved exact ID at Send and keep original family intent for display; retry/history must not re-resolve it against newer defaults.
- [ ] Without an explicit version, enable reasoning only when every advertised version has verified feature support and the same reasoning set, comparing sets independently of order. Any discrepancy or unknown capability disables explicit reasoning until a version is chosen. Reject stale saved reasoning that bypasses this control. Omitted effort continues to use configured policy and is omitted on the wire.
- [ ] On version changes retain explicit reasoning when the target supports it, otherwise reset it to provider default. Returning to unversioned selection applies the same uniform-set rule. Changing family still resets both version and reasoning.
- [ ] Verify controlled real-browser selection and outgoing requests for a non-global-default family, numeric/prerelease ordering, shuffled rows, ambiguous or opaque versions, unknown and unequal capability sets, saved settings, version retention/reset, immutable retries and omitted/default effort. Require focused mutations, exact applicable backend/frontend coverage, installed-package/relocation, licenses and public-boundary checks plus independent and external review.
- [ ] Human acceptance of this picker repair remains open.

This supersedes the earlier requirement to select an explicit version for every non-default family and the earlier intersection-only reasoning menu. It does not change provider capabilities, inference defaults, authentication or workflow qualification.

## Initial and reset default state

- [ ] Apply the identical-family reasoning rule to initial settings and Use backend default, where no family or exact model is explicitly saved. Resolve the effective family from the configured default row. Disable the menu for mixed or unknown sibling support and reject stale explicit efforts before sending. Explicit concrete IDs, pinned snapshots and family-less rows retain their own verified capabilities; omitted configured policy stays omitted on the wire.
