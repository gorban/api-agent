# Synthetic infrastructure resource pack

This independently authored conformance pack can be built as a wheel and relocated separately from the core installation. Its normal pack metadata declares captured image-native server assets and a persistent once-only seed marker. It makes no model calls.

Freshly resolved optional `USE_LOCAL_BACKING` selects three cases:

- Omitted: remote services, no managed prerequisites.
- `false`: local API using the explicit external backing address in `external.json`.
- `true`: local API plus managed backing; typed dependency output supplies the backing service's in-project endpoint.

The default loopback ports are 58090 and 58091. Disposable native proof may change those declaration/fragment ports before capture. Use the pinned public image already available on the selected explicit Unix engine; the fixture does not pull or start services during discovery or planning. `server.py` creates `/data/seed.json` only when absent and exposes HTTP identity and seed completion for validation on fresh acquisition and compatible reuse. Cleanup preserves named volumes.
