# Scoped local dependencies

`compose.json` is valid Compose YAML expressed as JSON. The pure planner selects
`langfuse`, `otel`, and `localstack` from explicit resolved configuration. Every
service has one profile and the three ownership labels. Compose itself supplies
project/service labels. All published ports bind `127.0.0.1`; databases, Redis,
ClickHouse, and the Langfuse worker have no host ports. Persistent data uses
project-scoped named volumes. Jaeger uses its default in-memory development
storage, so its traces reset when its container restarts.

The planner passes explicit project, project directory, file, and the controlled
empty env file. Its subprocess environment contains generated synthetic
scope-bound local development values, explicitly resolved LangFuse API keys and
`COMPOSE_DISABLE_ENV_FILE=1`. The lifecycle accepts an explicit allowlist of host
engine connection variables so a Docker-to-host-Podman wrapper can work. No
unselected dotenv values or inherited AWS/provider credentials are loaded. These public development
values are deliberately stable for an existing database; they are unsuitable for
production. The web service initializes a local organization, project, API keys,
and login via `LANGFUSE_INIT_*`. Values are passed directly, without literal
quote characters. The scope identity is based on both canonical roots.

`runtime/prerequisites.py` supplies bounded startup, readiness, cross-process
leases and shutdown. It compares exact service/image/scope/configuration before
reuse, uses `up --no-recreate` with health-gated dependencies, and rolls back only
newly created or restarted containers. Local release stops its own unleased
containers; test release retains the stack. Cleanup holds the same scope lock
through final lease/ownership checks and immutable-ID stop. Named volumes remain.
The journal lives under the active test-owner repository's ignored
`artifacts/prerequisites/`. Unverifiable leases refuse cleanup.

Digest-pinned images are verified against the locally inspected repository
digest and the running container's immutable image ID. Engines may normalize
the displayed image name; that alone does not establish compatibility.

Reusable operation startup is implemented; TUI execution consumers remain separate
tasks. Unit tests use controlled engine processes. Scoped backend diagnostics
verify S3 provisioning/restart history and actual traces from both exporters;
interactive integration/E2E verification remains open.
Config validation discards stdout/stderr to avoid exposing resolved secrets.

## Version and upstream evidence (verified 2026-10-06)

- [Langfuse v3.163.0 compose](https://github.com/langfuse/langfuse/blob/v3.163.0/docker-compose.yml):
  the web/worker v3.163.0 pair deliberately preserves compatibility with the
  pinned v3 deployment. It is pinned, rather than silently upgrading to
  the current v4 architecture. PostgreSQL, ClickHouse, Redis and MinIO are all
  included, with health-gated dependency startup.
- [Langfuse headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization):
  organization/project/user and API-key initialization dependency contract.
- [Current Langfuse compose](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)
  now uses v4 and ClickHouse 25.12. A future v4 upgrade should be deliberate.
- [Jaeger 2.21 getting started](https://www.jaegertracing.io/docs/2.21/getting-started/)
  and [download](https://www.jaegertracing.io/download/): supported v2 image
  `cr.jaegertracing.io/jaegertracing/jaeger:2.21.0`, with OTLP HTTP port 4318
  and browser UI port 16686. No obsolete `all-in-one` v1 image is used.
- Both the [v3.163.0 Langfuse compose](https://github.com/langfuse/langfuse/blob/v3.163.0/docker-compose.yml)
  and current upstream compose select Chainguard MinIO. The controlled profile
  pins its image digest and uses the included `mc ready local` health check.
  The previous official MinIO release image is no longer reachable from the
  tested registries.
- [LocalStack-persist v4.14.0](https://github.com/GREsau/localstack-persist/releases/tag/v4.14.0):
  Community-derived local S3 persistence, pinned by version and image digest.
  No host Docker socket or LocalStack account credentials are used. Its maintainer
  expects limited future updates after LocalStack's licensing changes; revisit
  this selection if maintenance or compatibility requires it.

The PostgreSQL 17.6, ClickHouse 25.3.6.56 and Redis 7.4.5 pins are conservative
compatibility selections, not claims to be their newest releases. Container
startup and fresh/existing-volume headless provisioning remain runtime checks.

## Managed local result history

`ALLURE_S3_USE_LOCALSTACK=True` selects local result storage with the declared
`allure-results` bucket and `http://localhost:4566` endpoint defaults. An explicit
bucket or region remains selected; `us-east-1` is the default region. The toggle
rejects an explicit conflicting endpoint before any engine command. With the
toggle False, an explicitly selected conventional local endpoint still selects
LocalStack; ordinary remote S3 remains independent.

`localstack-ready.sh` provisions the selected bucket with `awslocal`, using an
explicit container-local endpoint and synthetic `test` credentials. It checks
existence, creates only when missing, and verifies readability after creation.
Creation and final verification errors fail startup. Regions other than
`us-east-1` receive an explicit location constraint. The script never contacts
AWS. Its content digest and selected bucket/region participate in configuration
compatibility, so an existing stack with different initialization must be
reviewed instead of silently reused.

The public hook is the only permitted bind mount. The planner resolves its source
from checkout or installed package assets, validates and snapshots a real file,
and mounts it read-only at the fixed `ready.d` path using exact short `ro,z`
syntax. This supplies shared SELinux labeling through Podman-backed Compose;
the previously used long bind form did not relabel on that engine. No engine socket is
mounted. Readiness waits for S3 and the controlled automatic hook to complete
successfully; acquisition then executes the same idempotent hook inside the
inspected immutable container ID, including for reused services. This also
checks that the selected bucket remains readable before result publication.

The hook and mount contracts follow [LocalStack initialization hooks](https://docs.localstack.cloud/aws/customization/advanced/initialization-hooks/)
and [Compose bind mount options](https://docs.docker.com/reference/compose-file/services/).
Config-only validation permits only this controlled hook bind. Unit tests execute
the hook against a controlled fake `awslocal`; scoped live diagnostics also check
engine provisioning. Interactive integration/E2E acceptance remains separate.

The persistence image mounts `localstack-data` at `/persisted-data`, enables only
S3 (`PERSIST_DEFAULT=0`, `PERSIST_S3=1`) and flushes changed state every second
and on graceful shutdown. LocalStack Community's `PERSISTENCE=1` did not preserve
S3 objects in the previous image. Retained history requires successful restart
retrieval, not just successful upload; a force-kill cannot guarantee the latest
unflushed write.
