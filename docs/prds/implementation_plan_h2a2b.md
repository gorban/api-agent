# Local Jaeger persistence and viewer port

[Shared reporting requirements](implementation_plan_h2a2.md)

## Bounded requirements

- [x] Keep the pinned Jaeger 2.21 all-in-one service under the OTEL profile and UID 10001. Reject service declaration drift, including image, entrypoint, environment, user and additional overrides. Configure its supported Badger backend with non-ephemeral storage and a project-scoped, labelled named volume. Retain the volume through normal stop and container recreation, with the reviewed seven-day span TTL.
- [x] Provide the Jaeger v2 configuration as a controlled packaged asset. Require the exact reviewed query extension configuration, preserving the pinned image’s default viewer binding; reject endpoint and bind overrides. Validate its exact mount and bytes in the source checkout and installed wheel; reject arbitrary or shadowing mounts and unscoped volumes.
- [x] Declare a review-visible local Jaeger viewer port, defaulting to 16686, while preserving the fixed loopback OTLP ingestion endpoint and exact reviewed container-port mappings. Require the published receiver to feed the persistent storage exporter through the controlled active service graph; declared but disconnected storage is insufficient. Reject invalid ports and collisions among selected Jaeger ingestion, viewer and LocalStack publications before startup; leave external trace endpoints unmanaged.
- [x] Verify rootless write permissions and trace retrieval after isolated stop and recreation with disposable data. Do not modify existing services or volumes. Keep broader reporting session ownership, management, restore and UI work in their own requirements.
- [x] Human acceptance: review a bookmarked viewer endpoint and retained disposable trace after recreation.

The [Jaeger 2.21 Badger guide](https://www.jaegertracing.io/docs/2.21/storage/badger/) documents single-node persistent storage and its non-root permission constraint. The [pinned sample configuration](https://github.com/jaegertracing/jaeger/blob/v2.21.0/cmd/jaeger/config-badger.yaml) describes v2 Badger keys, values, expiry and exporter wiring.

Isolated rootless verification sent a synthetic OTLP span to the managed service, queried it through Jaeger before and after normal release, container removal and recreation, and confirmed that the new container read the original trace from the same named volume. Both disposable containers and the volume were removed afterward. A built wheel included all controlled Compose assets and resolved them from an unrelated working directory; its Compose configuration validated.

Technical implementation and independent/external review are complete. The final joined 32-worker gate passed 10,955 tests and 27 subtests at exact measured line and branch coverage, with installed-package, license and public-boundary checks. Older managed reporting bindings keep their original asset identity; this slice does not silently migrate an existing installation when the asset bundle changes.

## Exact storage exporter declaration

- [x] Require the active Jaeger storage exporter mapping to match the reviewed declaration exactly: `jaeger_storage_exporter` with `trace_storage: local_traces` and no extra exporter keys or options. In particular, reject an enabled storage-exporter queue, which changes when an accepted span reaches Badger. Cover this with a controlled config mutation and a check that disabling the exporter guard makes the rejection test fail.

The user accepts the delivered reporting-management scope. This records approval without inventing additional manual/native observations or accepting unfinished broader lifecycle, migration or destructive-volume features.

## Retained service compatibility

- [x] Reuse an unchanged owned Jaeger container when unrelated services change the current Compose bundle or add assets absent from its retained snapshot. Verify the exact Jaeger service fingerprint and image, mounted configuration bytes, read-only bind, safe owned ancestors and file permissions, and writable scoped labelled Badger volume. Retain the original container, snapshot identity and data without migration.
- [x] Cover absent unrelated assets and unrelated Compose declarations with reuse tests, retain rejection tests for relevant configuration and ownership drift, and prove the configuration-byte rejection is causal by disabling only its byte comparison.
- [x] Independent review and joined integration gate for retained service compatibility.

This service-scoped requirement supersedes the earlier whole-bundle compatibility policy only for an unchanged Jaeger service. It does not migrate the retained snapshot or data.

- [ ] Human acceptance: review reuse of an existing unchanged Jaeger service after an unrelated service bundle update. Earlier acceptance does not cover this addition.
