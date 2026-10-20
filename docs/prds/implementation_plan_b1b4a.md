# Selected workflow-library dependency admission

Source execution supports distinct runtime, resource-pack and selected workflow-library roots.

- [x] Read only the admitted `workflow_base_root`, runtime and pack project manifests. Resolve aliases and read each project once; never discover sibling projects or search parent directories.
- [x] Parse project dependencies and requested optional groups without importing factories or native packages. Apply existing PEP 508 and direct-URL rejection before installation.
- [x] Preserve optional-group validation, source-provided `api-agent` omission, selected provider manifests and collection tools.
- [x] Re-read source manifests each preparation so edits immediately change requested installation.
- [x] Tolerate absent library attributes and absent project metadata. Installed packages receive dependencies through normal distribution installation; this collector neither fabricates project metadata nor searches parent directories.
- [x] Synthetic regression tests verify distinct roots, aliases, working-directory isolation, selected versus unselected roots, malformed requirements, groups and exact mocked installer arguments.
- [ ] Human acceptance with a separately configured source library and its actual external dependencies.

Implementation: `src/api_agent/runtime/dependencies.py`. Tests: `tests/unit/test_workflow_library_dependencies.py`.

Source identity requirements:

- [x] Satisfy references to the admitted source project's normalized name locally only when its static version satisfies the parsed requirement. Reject incompatible or unavailable versions before installer execution.
- [x] Evaluate local reference markers; expand requested declared extras, including their applicable marked children, without silently discarding extra dependencies. Reject unknown extras and direct URLs; prevent recursive extra cycles.
- [x] Retain installed-library requirements for normal package installation. Never infer local satisfaction from installed resource inventories or search other roots.
- [x] Independent implementation review of source identity satisfaction and installed delivery.

- [x] Normalize requested extras for declaration lookup, child marker evaluation and cycle tracking. Reject colliding normalized extra declarations, including identical duplicate values, rather than choosing one declaration.

## Delivery verification

- [x] Independent source-dependency and installed-delivery review, including normalized extras, ownership and installed resource rejection controls.

The complete applicable runtime suite and relocated installed artifact checks pass. Human acceptance above remains open.
