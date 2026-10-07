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

Operation startup and TUI test execution wiring remain separate tasks. Unit
tests use controlled engine processes; actual engine startup, provisioning,
tracer export and retained history still require integration verification.
Config validation discards stdout/stderr to avoid exposing resolved secrets.

## Version and upstream evidence (verified 2026-10-05)

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
- [MinIO release history](https://github.com/minio/minio/releases): the dated
  `RELEASE.2025-04-22T22-12-26Z` image is an explicit compatibility pin.
- [LocalStack releases](https://github.com/localstack/localstack/releases/tag/v4.14.0):
  explicit community-development compatibility pin 4.14.0 for S3 only; no host
  Docker socket is mounted and no LocalStack account credentials are used.

The PostgreSQL 17.6, ClickHouse 25.3.6.56 and Redis 7.4.5 pins are conservative
compatibility selections, not claims to be their newest releases. Container
startup and fresh/existing-volume headless provisioning remain runtime checks.
