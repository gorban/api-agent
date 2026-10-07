# Strict Markdown executable Layout

## Checklist

- [ ] Use `layout.md` for LangGraph orchestration, with a versioned, deliberately restricted Markdown grammar. Review the exact allowed headings/table columns with the user before implementing the loader. Configuration and pack-manifest formats remain separate choices.
- [ ] Parse Markdown structure into a typed Layout definition rather than treating arbitrary prose as instructions or extracting executable behavior with loose text matching. Accept only declared structural forms: fixed headings, fixed-column tables, inline identifiers, and inline file links; reject unknown executable sections/columns, duplicate IDs, invalid field types, unresolved functions/bindings, and ambiguous declarations with file/line diagnostics.
  - [ ] Represent nodes, unconditional/conditional edges, input/output bindings, independent loop groups/attempt budgets, and ordered conditional prompt sources using the same strict grammar. Named conditions resolve to registered Python functions; table cells do not contain arbitrary Python expressions.
  - [ ] Define optional nonexecuting notes explicitly so explanatory prose cannot accidentally alter the graph. Generate diagrams from the parsed definition; Markdown itself is the executable readable description, not a separate copy of the flow.
- [ ] Store local prompt links as genuine inline Markdown links, with destinations always relative to the Markdown declaration/fragment containing the link. Resolve runtime paths against that file's parent directory, never the process working directory, pack root by accident, or repository root by convention. Preserve declaration-file provenance through includes.
  - [ ] Keep prompt row order as application order. An unconditional entry uses an explicit `always` marker; a conditional entry names its condition function. Validate every local link in the selected declaration, including conditional entries, before building the graph; report broken/invalid paths rather than silently omitting prompts.
  - [ ] Require relative local destinations and readable existing prompt files. Use normal portable URI escaping for filenames with spaces/special characters and normalize paths through a defined decoder. Do not confuse heading anchors, external URLs, or directory links with executable prompt files.
- [ ] Preserve `<resource_pack>` runtime path support while distinguishing it from a genuine GitHub file link: GitHub/editor navigation cannot resolve a runtime-selected root by itself.
  - [ ] Put pack-specific prompt sections in pack-owned Markdown declarations/fragments alongside their node responsibilities, with real relative links to local prompt files. Resolve fragment selection via `<resource_pack>` at runtime, then resolve links relative to that fragment. For example, `<resource_pack>/workflows/rendering/renderer/prompts.md` can contain links such as `prompts/say_ok.md`.
  - [ ] Prefer this fragment pattern over presenting variable-containing destinations as working hyperlinks. If showing resolved pack views/previews, generate them from the same parsed definitions; do not maintain a second set of paths by hand. A link to a file in a different repository needs an explicit cross-repository navigation strategy; ordinary same-repository links remain declaration-relative.
- [ ] Add deterministic parser/link validation for the installed engine and demo pack, selected resource pack, differing working directories, renamed/missing prompt files, conditional order, and resource-pack fragment switching. Run Layout validation as a targeted developer check without executing an LLM. Only claim links are valid after their files exist and validation passes; the initial planning skeleton does not create them yet.

## Illustrative readable declaration

The exact schema will be reviewed before implementation. This example is located conceptually at `api-agent/demo/layout.md`; its relative paths refer to the planned skeleton, not to this planning document.

```markdown
# LangGraph Layout v1

## Nodes

| ID | Function |
| --- | --- |
| renderer | render_response |

## Edges

| From | To | When |
| --- | --- | --- |
| START | renderer | always |
| renderer | END | always |

## Prompts

| Node | File | When |
| --- | --- | --- |
| renderer | [Response contract](../src/api_agent/workflows/rendering/renderer/prompts/response_contract.md) | always |
| renderer | [Say Ok](workflows/rendering/renderer/prompts/say_ok.md) | always |
```

[GitHub's relative-link documentation](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#relative-links) confirms that Markdown links are relative to their containing file and follow the viewed branch. Editor link navigation depends on the editor's Markdown support; runtime validation supplies the executable-path guarantee.
