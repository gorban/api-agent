# Repository-rendered fallback envelopes

[Workflow presentation](implementation_plan_b1a3.md)

- [x] Escape path quotes in root-document fallback HTML so nested viewing commands do not break the surrounding Markdown reference title or expose bootstrap/carrier text in repository previews. Preserve literal double quotes in editor instructions and the displayed browser command.
- [x] Keep existing published v2 envelopes readable and preserve executable bytes, line maps, CSP and inert carriers. Apply the shared envelope fix to all owned flow roots and regenerate derived resources.
- [x] Verify reference-definition consumption for root/nested/special-character paths, browser fallback text, direct/managed viewing and existing source contracts. Complete applicable coverage, provenance, boundary and external review. Repository-platform human acceptance remains separate.

- [ ] Human acceptance of the repaired repository previews.

Verification: 23,621 unit tests passed with exact 100% line/branch coverage. The old-behavior causal control reproduces the three nested-path failures while both root-path cases pass. GitHub GFM rendering, direct-file browser fallback, the two-case recursive managed-view integration audit with Allure publication, installed/relocated checks and independent external review pass. Existing published envelopes retain their executable bytes and line maps.
