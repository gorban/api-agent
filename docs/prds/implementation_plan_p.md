# Mise startup and conditional prerequisite preparation

[Main checklist](implementation_plan.md)

## Outcome and scope

A user can download and extract the repository ZIP, follow mise's official setup instructions, and open the terminal workbench through one root `launch.sh`. The launcher acquires the selected tool versions and packages when needed. Users do not need an IDE, a Markdown renderer, preinstalled Python, or a Git checkout to begin.

Mise is the required tool prerequisite. A working supported container engine and client/Compose capabilities are conditional. Host browser support, certificate trust and external account access remain distinct capabilities checked when needed; this is not a promise that installing mise satisfies every operation on every machine.

## Requirements

- [ ] Deliver [first-run instructions, archive support and transparent launcher migration](implementation_plan_p1.md), using the [agreed README Markdown](implementation_plan_p1a.md), migrating generated workflow-viewing instructions, and keeping `launch.sh` as the sole root launcher with Python entry code under `src/api_agent/`.
- [ ] Deliver [operation-specific preparation, Docker conditions and contextual remediation](implementation_plan_p2.md), reusing existing ownership, progress and cleanup contracts.
- [ ] Qualify the [base executable inventory](implementation_plan_p3.md), preserving existing discovery/admission paths, and the subsequently requested [selected-tool recursive executable extension](implementation_plan_p3a.md).
- [ ] Verify the stated entry and operation combinations, applicable exact unit line/branch coverage, focused mutations where required, independent review and human acceptance. Keep implementation and acceptance unchecked until evidenced.

This task changes onboarding and prerequisite preparation. It does not add a bundled executable distribution, a new service orchestrator, a universal operating-system installer, or implementation of a pack's unfinished API/database services. Existing [infrastructure contracts](implementation_plan_o1.md), [runtime ownership](implementation_plan_h.md), [frontend integration](implementation_plan_i2.md) and [documentation requirements](implementation_plan_c.md) continue to own their respective behavior.

These requirements supersede earlier root-`launch.py` and mandatory-Git startup assumptions for this migration; retain Git worktree protections for actual checkouts. Preserve CLI/configuration behavior and existing execution, privacy, coverage and human gates. The surgical scope retains the existing Python environment and resolver; control environments, operation workers, collection locks, receipts and process-fence protocols are not required.

## Implementation status

The shell/internal-entry migration, README setup, generated viewing guidance and
selected executable preparation are implemented. Node/npm preparation includes
the bundled GraphQL MCP owner as well as frontend setup. Existing environments
using another or unavailable Python are preserved and rejected with recovery guidance.

Verification: 21,011 units plus 27 subtests passed, followed by 150 final targeted
GraphQL/entry/environment checks; combined measured statement/branch coverage is
100%. Extracted-source startup worked without host language runtimes or Git.
Generated lock entries do not establish fresh-host/non-Linux qualification.
Platform, publication and human acceptance remain explicit. The separately
requested recursive executable extension has its own linked verification gates.
