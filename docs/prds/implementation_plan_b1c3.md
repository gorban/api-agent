# Historical workflow source identity

[Inspector](implementation_plan_b1c.md) · [Execution evidence](implementation_plan_b1c1.md) · [Recorded viewer](implementation_plan_b1c2.md)

Bind historical execution overlays to the declaration documents actually consumed by that invocation. A commit reference helps locate source; verified content establishes a match. This work extends the existing source/resource fingerprints and does not replace execution qualification or full runtime provenance.

## Capture and deterministic identity

- [ ] At compilation, freeze the selected entry and every Markdown declaration/resource actually consumed, including nested documents and resolved pack overrides. Compute fingerprints from those same captured bytes rather than rereading mutable files during publication or viewing. Preserve selected-root, replacement and resolved-graph identities separately.
- [ ] Define a versioned SHA-256 document manifest: logical owner namespace, owner-relative normalized path, byte length and SHA-256 for each consumed document. Sort entries by owner/path with a specified deterministic comparison and canonical serialization before hashing the manifest. Preserve document byte order and contents; sorting must not reorder graph declarations. Reject duplicate/ambiguous normalized paths. Absolute machine paths and repository credentials are not provenance identifiers.
- [ ] Store this aggregate hash and per-document entries in the manifest-hashed Allure execution artifact, together with each owning repository's full commit object ID and object format when available. Record source availability and whether the consumed bytes match that commit. A pack/provider checkout needs its own provenance; the application repository's SHA alone cannot describe it. Gitless installed resources remain usable with explicit unavailable Git provenance and existing package identity.
- [ ] Bind the executed resolved graph projection and generated view to the captured declaration identity. Record parser/renderer versions or fingerprints separately; generated HTML is not the declaration source of truth. A Markdown-only digest does not establish Python implementation equivalence: callable links must disclose their independent source identity or that historical implementation was not verified. Preserve existing broader execution fingerprints.

## Uncommitted source policy

- [ ] Compare consumed documents with their recorded commit's blobs. Distinguish modified, staged-only, untracked, missing and unverifiable inputs as relevant; staged does not mean committed. Unrelated repository edits must not make matching workflow documents fail. Do not automatically commit, stash, reset or alter the working tree.
- [ ] Keep ordinary development tests usable with uncommitted workflows. Record an explicit non-committed/unverifiable source state and a clear warning when commit-based reconstruction will be unavailable. A permitted frozen graph projection can still support historical inspection. Capturing document bodies remains subject to privacy/capture limits; this feature does not authorize raw source or secret capture. A scrubbed projection cannot claim byte-exact original source.
- [ ] At contract freeze, assess an opt-in committed-workflow prerequisite for callers requiring commit-reconstructible history. It would reject before test execution/inference with the affected safe workflow paths and an actionable message covering staged and untracked inputs too. Do not silently introduce a default commit-before-testing restriction.

## Historical lookup and rendering

- [ ] Open a previous test/attempt against its captured projection or declaration source verified at the recorded commits. Resolve objects only in explicitly mapped local repositories/approved artifact stores; read without checking out or modifying the user's working tree. Verify every selected path/blob and the aggregate digest, plus resolved override identity. Never substitute today's flow because the commit, object or content is missing or mismatched.
- [ ] Show verified source, captured projection, partial or unavailable states distinctly. Missing Git objects or dirty-source capture must not hide otherwise valid test outcomes, accounting or trace details. If current workflow bytes happen to match the recorded digest, a verified content match may be shown with its actual provenance; do not relabel them as the recorded committed source.
- [ ] Treat recorded paths, commits and Markdown as untrusted data. Bound Git/object/document work and reject traversal, unsupported object types and unsafe links. Historical viewing cannot fetch arbitrary remotes, execute linked Python, authenticate, compile active provider code or initiate inference. Render historical declarations through the existing safe read-only viewer contracts.

## Acceptance

- [ ] Verify stable hashes under enumeration changes and sensitivity to changed bytes, paths, nested resources and selected overrides. Exercise multiple repositories, missing Git metadata, staged/unstaged/untracked documents, unrelated dirty files, mid-run edits and changed HEAD. Verify capture and execution use the same frozen source.
- [ ] Replay exact historical commits, missing/rewritten objects, forged blob/aggregate hashes, unavailable snapshots, scrubbed captures, traversal and bounded oversized data. Confirm no current-source substitution, working-tree mutation or code execution. Mutations must detect omitted document/override identity checks and commit-blob verification. Renew applicable independent, package and public-boundary gates when implemented; these requirements are future work.
