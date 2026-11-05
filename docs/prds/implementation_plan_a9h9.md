# Concurrent lock creation and isolated clipboard controls

[Native cleanup requirements](implementation_plan_a9h8.md)

- [x] Create credential lock files with an exclusive descriptor-relative create, opening an existing file only after an already-exists result. Preserve the same directory descriptor, no-follow flags, private ownership, permissions, regular-file and single-link checks. Reads and already-exclusive writes retain their existing behavior.
- [x] Never retry arbitrary filesystem failures, reopen a changed directory, or recreate a lock deleted between creation and existing-file open. Preserve safe stage/errno diagnostics and fail closed for missing directories, symlinks, unsafe metadata and interrupted operations.
- [x] Keep synthetic clipboard subprocess mocks local to the clipboard owner. Actual worker cleanup must retain trusted system process inspection on platforms without procfs.
- [x] Require causal rejection controls, independent adversarial and external review, the full applicable unit suite with exact line/branch coverage, and public-boundary/index/metadata checks before publication. Preserve worker defaults and production deadlines.
- [x] Native verification through the normal unit launcher: 26,887 passed and 16 platform-incompatible tests correctly not applicable at 16 workers. The supplied native log reports exact 100% line and branch coverage.

The preceding native run reported exact 100% line and branch coverage, with eleven failing tests. Ten native clipboard/login failures share a global subprocess mock; forcing system process inspection reproduces all ten and isolating the mock repairs them. Concurrent first lock creation fails with an open-stage missing-file error; the bounded creation protocol avoids the ambiguous create-or-open operation while retaining security checks.

Verification: 26,903 Linux unit tests pass at 16 workers with exact 100% line and branch coverage. Independent adversarial and external source reviews pass; focused mutations reject removal of the creation protocol, owner and symlink checks, and existing-only fallback. Workflow and staged public-boundary checks pass. The subsequent native normal-launcher run passes in 7m32 with all eleven reported failures resolved; exact coverage is reported in its log, without separately supplied coverage artifacts.
