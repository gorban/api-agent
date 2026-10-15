# Remaining shared-resource coordination

[Shared resources](implementation_plan_h2.md).

## Future decisions and implementation

- [ ] Define shared tracing project/run attribution, ingestion access and data-retention policy. Persistent collectors and supporting databases need canonical service ownership; invocation cleanup must not stop collectors still needed by another session or erase traces. The last-session lifecycle owner stops managed collectors while retaining durable storage; [Jaeger persistence and shared management](implementation_plan_h2a2.md) are explicitly required.
- [ ] Define application database ownership, migration exclusivity and fixture isolation. Fixed application endpoints do not make simultaneous destructive tests safe; preserve the current execution guard until the relevant isolation is proved.
- [ ] Plan account-scoped authentication state and browser-profile ownership. Coordinate mutable sessions, token refresh and profile cleanup; keep independent accounts separate and credentials outside shared reporting/cache artifacts.
- [ ] Make parallel quota accounting a separate decision. The current machine-local suite lock cannot account for unrelated consumption on another computer; require provider/account/window attribution and explicit uncertainty before approving parallel measurements.
- [ ] Preserve exact invocation ownership of workers, process groups, temporary staging, browser inspection and cleanup duties. Shared durable services have their own owner; port or health matches must never transfer ownership. Pending cleanup retains its lease until acknowledged or the owner process terminates.
- [ ] Establish compatible versions, schema/service upgrade sequencing and recoverable backups for every persistent resource. Verify restore compatibility and retained identities before retiring older storage; resets and destructive cleanup remain explicitly authorized operations.
- [ ] Define permission boundaries for shared developer services and machine-local caches. Separate publication access from administration and prevent credential or private account evidence from being exposed through shared history.
- [ ] Independently review resource failure/recovery combinations with synthetic services and accounts before requesting human acceptance. Keep runtime implementation and live verification unchecked until completed.
