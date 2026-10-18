# Verified retained LocalStack plan reuse

[Shared reporting lifecycle](implementation_plan_h2a2.md)

- [x] Reuse an existing registered LocalStack object when a newer packaged asset bundle changes only inactive Compose or Jaeger bytes and the selected LocalStack service, initialization hook, configuration, and recorded fingerprint remain exact. Support older three-asset registrations without inventing a missing Jaeger asset.
- [x] Under the registry and original ownership locks, verify the recorded engine generation, container ID, image, profile, labels, selected service fingerprint, bind source and captured file bytes, volume and journal. Read retained files through bounded, no-follow descriptors and compare their hashes to the original registry. Preserve the original registry and snapshot bytes. Never invoke Compose up or a new consumer initialization/exec path; a same-ID restart retains the original image startup and its verified identical hook.
- [x] Running objects are reused and stopped objects restart by their verified immutable ID. Missing, replaced or incompatible objects refuse before Compose creation. Keep Jaeger startup and broader asset-bundle migration separate. Refuse changed hooks, selected service, owner, mount, volume, or registry facts without altering the engine or registry.
- [x] Prove exact running and stopped reuse, refusal cases, cleanup/retry, and a bypass mutation with controlled engine and filesystem fixtures. No live service is needed for automated proof.

- [x] Human acceptance: rerun a previously blocked selection using a verified retained installation. Review refusal and interruption proofs without changing real data.

Human acceptance of this delivered LocalStack scope is recorded. This does not accept broader tracing, management, destructive storage actions, or additional native-platform behavior. No additional manual observations are inferred from that signoff.
