# Workflow-library installed delivery

- [x] Admit repeated distribution discovery only when physical METADATA and RECORD identity, metadata content, and complete file inventory agree. Distinct installations and unverifiable aliases fail closed without importing package code.
- [x] Ignore parent-prefixed RECORD entries resolving outside the installation root, supporting standard installed console scripts. Preserve strict selected resource containment, owned-file validation, internal symlink rejection, and invalid-source precedence.
- [x] Exercise real filesystem distribution metadata with synthetic inventories and physical aliases; retain conflicting and unavailable inventory rejection controls.
- [x] Independent implementation review of alias, inventory and source-dependency repair.
- [ ] Human acceptance with a standard environment and an installed workflow library.

Implementation: `src/api_agent/runtime/resource_pack.py`. Regression controls: `tests/unit/test_workflow_library_distribution_delivery.py`.

- [x] Bind duplicate discovery to the filesystem provider's actual metadata directory and its METADATA/RECORD files. Inventory entries naming another metadata directory cannot establish provider identity; unverifiable custom providers remain ambiguous. Distinct metadata directories remain distinct even when metadata files are hardlinked.

- [x] Reject internal traversal entries even when they resolve to existing selected files; retain outside console-script compatibility. A focused guard mutation must make the internal-traversal rejection test fail.
- [x] Exercise source-root containment, unreadable provider identity, out-of-root symlink inspection and rejection of external conditions for declarative stateful workflows.
