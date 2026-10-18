# Local Jaeger persistence and viewer port

[Shared reporting requirements](implementation_plan_h2a2.md)

## Bounded requirements

- [x] Keep the pinned Jaeger 2.21 all-in-one service under the OTEL profile and UID 10001. Reject service declaration drift, including image, entrypoint, environment, user and additional overrides. Configure its supported Badger backend with non-ephemeral storage and a project-scoped, labelled named volume. Retain the volume through normal stop and container recreation, with the reviewed seven-day span TTL.
- [x] Provide the Jaeger v2 configuration as a controlled packaged asset. Require the exact reviewed query extension configuration, preserving the pinned image’s default viewer binding; reject endpoint and bind overrides. Validate its exact mount and bytes in the source checkout and installed wheel; reject arbitrary or shadowing mounts and unscoped volumes.
- [x] Declare a review-visible local Jaeger viewer port, defaulting to 16686, while preserving the fixed loopback OTLP ingestion endpoint and exact reviewed container-port mappings. Require the published receiver to feed the persistent storage exporter through the controlled active service graph; declared but disconnected storage is insufficient. Reject invalid ports and collisions among selected Jaeger ingestion, viewer and LocalStack publications before startup; leave external trace endpoints unmanaged.
- [x] Verify rootless write permissions and trace retrieval after isolated stop and recreation with disposable data. Do not modify existing services or volumes. Keep broader reporting session ownership, management, restore and UI work in their own requirements.
- [ ] Human acceptance: review a bookmarked viewer endpoint and retained disposable trace after recreation.

The [Jaeger 2.21 Badger guide](https://www.jaegertracing.io/docs/2.21/storage/badger/) documents single-node persistent storage and its non-root permission constraint. The [pinned sample configuration](https://github.com/jaegertracing/jaeger/blob/v2.21.0/cmd/jaeger/config-badger.yaml) describes v2 Badger keys, values, expiry and exporter wiring.

Isolated rootless verification sent a synthetic OTLP span to the managed service, queried it through Jaeger before and after normal release, container removal and recreation, and confirmed that the new container read the original trace from the same named volume. Both disposable containers and the volume were removed afterward. A built wheel included all controlled Compose assets and resolved them from an unrelated working directory; its Compose configuration validated.

The joined 32-worker unit gate passed 10,884 cases and 27 subtests at exact measured line and branch coverage, with installed-package, license, public-boundary and independent review checks. Older managed reporting bindings keep their original asset identity; this slice does not silently migrate an existing installation when the asset bundle changes.
