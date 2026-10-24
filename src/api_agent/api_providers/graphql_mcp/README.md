# GraphQL MCP runtime asset

`package-lock.json` pins `mcp-graphql@2.0.4` and its full npm dependency graph. At an explicit API setup boundary, call `prepare_assets(managed_directory)` from this package. It copies the lock and runs bounded `npm ci --ignore-scripts --no-audit --no-fund` into that directory. The owner receives the same directory through `assets_dir`. It launches the installed package's shipped `dist/index.js` with a resolved Node executable. There is no install or download during discovery, validation, or invocation. Preparation and launch verify pinned package/lock fingerprints and the exact installed dependency-tree content; modified JavaScript or dependency files fail closed. The child runs with a bounded Node old-space heap.

The package's MIT license and dependency notices are retained under `licenses/`. The installed tree remains with the managed runtime artifact; `node_modules` is ignored by Git and excluded from source distributions.

Fresh provider instances verify and reuse an intact installation. Registry access
and the npm cache are unnecessary for that reuse; only a missing or invalid tree
triggers explicit preparation.

Each invocation borrows a fresh schema view over the owned stdio child. The view sends fixed introspection through MCP, publishes a bounded complete SDL to the planner, and freezes its fingerprint for validation. Configured header values and Authorization Bearer/Basic tokens are redacted recursively from returned data and errors before the renderer receives them.
