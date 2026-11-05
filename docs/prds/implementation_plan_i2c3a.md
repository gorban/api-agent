# Studio model-label capitalization

[Application catalog contract](implementation_plan_i2c3.md)

- [x] Format Studio model-group labels consistently after concise alias selection. For each word-like part, case-insensitive `gpt` becomes `GPT`, `claude` becomes `Claude`, and every other part uses an uppercase first letter and lowercase remainder. Preserve separators and numeric portions. Examples: `GPT-Terra`, `Claude-Sonnet`.
- [x] Keep formatting in the bundled Studio presentation. Preserve raw catalog labels, full model IDs, family handles, versions, option values, provider metadata and alternate UI freedom.
- [x] Perform duplicate-name detection after formatting. If capitalization creates a collision, retain distinct choices with the original exact model ID suffix; never apply the label formatter to that suffix.
- [x] Verify mixed upstream capitalization, version-label disagreement, aliases, scoped variants, unknown/no-family labels, empty labels and case-normalized collisions without paid inference. Complete independent/external review, applicable exact frontend and Python unit line/branch coverage, and public index/metadata checks before publication.
- [x] Human-approved the consistent Studio model list.

Verification: 283 frontend tests plus five instrumented checks pass with exact authored and Node line/branch coverage; seven controlled original-page tests also pass. Independent adversarial and external source reviews pass. The cleared full Python unit gate passes 26,915 tests at 16 workers with exact 100% line and branch coverage. Public-boundary and workflow checks pass; human approval of the implemented model-list presentation is recorded.
