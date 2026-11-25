# Docker and optional prerequisites

Follow [Get started](../README.md#get-started) to install mise, the always-required
tool prerequisite. Additional requirements depend on the operation you select.

## When Docker is needed

Docker is an optional development dependency. Install and start an accessible
supported engine, with its client and Compose support, when selecting any of these:

- Managed local Jaeger or LangFuse tracing, including their backing services.
- The shared managed LocalStack S3 service (see [Shared S3](#shared-s3-localstack)): always for
  Run local, and for recorded integration/E2E Allure reporting, the Allure viewer/restore, and
  unit runs whose selected tests carry a marker mapped to `api_agent:shared_s3`.
- A containerized local API and its dependencies.
- A provider, workflow tool, or test that requires containers.
- Container image builds or container-based tests.

External tracing, external S3 storage (`S3_USE_LOCALSTACK=False`), and an external API each remove the need
for their corresponding local stack. Disabled tracing needs no tracing stack.
Another selected feature can still require Docker. The launcher manages the
selected application containers; it does not install or start your Docker engine.

## Free engine options

Choose one; follow its upstream installation instructions for your platform:

- [Podman](https://podman.io/docs/installation): a free, open-source engine.
  [Podman Desktop](https://podman-desktop.io/docs/installation) is an optional
  free, open-source graphical setup and management interface.
- [Standalone Docker Engine](https://docs.docker.com/engine/install/): the
  open-source Linux engine, installed independently of Docker Desktop. Also
  install its [Compose plugin](https://docs.docker.com/compose/install/linux/).

Docker Desktop also works: it reports bind mounts as its VM sees them
(`/host_mnt/...`), and retained-container verification maps those back to host
paths only when the engine identifies itself as Docker Desktop. These options do
not require Docker Desktop. Container-registry accounts,
download limits and hosted services have separate terms from the engine.

### Using Podman with Docker commands

The launcher invokes a `docker` executable, including `docker compose`. Podman
can provide that interface. Follow its
[Docker CLI compatibility guide](https://podman-desktop.io/docs/migrating-from-docker/aliasing-the-docker-cli)
and [Compose setup](https://podman-desktop.io/docs/compose/setting-up-compose).
Compose is a separate provider; installing Podman alone does not install it.
The provider must support Docker Compose options, including
`--project-directory`. Docker Compose itself can use Podman's socket; a
`podman-compose` version that rejects these options is not sufficient.

For commands you type in a POSIX-compatible interactive shell, an alias is handy:

```sh
alias docker=podman
```

However, shell aliases are not inherited by the launcher's Python subprocesses.
Use your platform's executable Docker-compatibility package (often named
`podman-docker`), or create a file named `docker` in a directory on your `PATH`,
such as `$HOME/.local/bin`, containing:

```sh
#!/bin/sh
exec podman "$@"
```

Make that file executable (`chmod +x "$HOME/.local/bin/docker"` for the example
location), and ensure its directory is on `PATH`. Do not overwrite an existing
Docker executable. Putting this directory first on `PATH` selects Podman ahead
of other Docker installations; skip the wrapper when you intend to use Docker.
This wrapper forwards arguments unchanged; `docker compose`
becomes `podman compose`, which still needs its separate Compose provider.

Also enable Podman's local API socket so Compose and the launcher's engine
verification reach the same engine. Follow the
[socket setup](https://docs.podman.io/en/stable/markdown/podman-system-service.1.html)
or [Podman Desktop compatibility settings](https://podman-desktop.io/docs/migrating-from-docker/managing-docker-compatibility).
If needed, set `DOCKER_HOST` to that socket's `unix://` URL using the
[upstream guidance](https://podman-desktop.io/docs/migrating-from-docker/using-the-docker_host-environment-variable).
Do not expose an unauthenticated engine socket over the network.

From the same environment where you will run `./launch.sh`, check:

```sh
env docker info
env docker compose --project-directory "$PWD" version
```

Using `env` bypasses shell aliases and checks the executable the launcher can
actually invoke. Both commands should succeed without `sudo`; a missing Compose
provider or inaccessible engine needs fixing before selecting container features.
Shared local report infrastructure additionally requires a local Unix socket,
not an arbitrary remote engine endpoint.

## Shared S3 (LocalStack)

One launcher-level S3 prerequisite serves every S3 consumer: Allure result history,
the application's data bucket, and pack tests. It is a disposable
`gresau/localstack-persist` container on the persistent named volume
`localstack-data`, shared per container engine and endpoint across checkouts and
modes through the original lease journal (`SharedS3Manager`, registry in
`~/.cache/api-agent/reporting`). Its entrypoint installs a start hook that keeps
a rejected conditional write (`If-None-Match`/`If-Match`) from emptying the
existing object, which the persistence image would otherwise do; see the
[Compose asset notes](../infra/compose/README.md#shared-s3-localstack).

Settings (all apply in unit, integration, e2e, allure and run-local modes):

| Setting | Default | Meaning |
| --- | --- | --- |
| `S3_USE_LOCALSTACK` | `True` for Run local and unit; explicit choice required for recorded integration/E2E | `False` starts nothing and uses `S3_ENDPOINT` as-is (opt-in to external S3). |
| `LOCALSTACK_PORT` | `4566` | Loopback host port. Set `S3_ENDPOINT` to the same port when overriding. |
| `S3_ENDPOINT` | `http://localhost:4566` when LocalStack is on | Endpoint every consumer uses; unset with `False` means AWS. |
| `S3_REGION` | `us-east-1` | Signing region; a running LocalStack with another region is refused. |
| `S3_EXTRA_BUCKETS` | `application-data` | Comma-separated non-Allure buckets (the application's data bucket). |
| `ALLURE_S3_BUCKET` / `ALLURE_S3_PREFIX` | `allure-results` / `allure` | Allure-specific; the bucket is added to the managed list. |

`ALLURE_S3_USE_LOCALSTACK`, `ALLURE_LOCALSTACK_PORT`, `ALLURE_S3_ENDPOINT` and
`ALLURE_S3_REGION` were renamed to the first four shared names above. Leaving an old name
in `.env` or the environment is a configuration error naming its replacement.

Lifecycle: the last consumer to release removes the container and its network
(never the `localstack-data` volume; no `volume rm` or `down -v` is ever issued);
acquiring again provisions a fresh container on the same volume, so stored
objects survive. Buckets are not part of the container's configuration: every
acquire, including on an already-running container, creates any bucket the
consumer lists that is missing (`SA_S3_BUCKETS` through `localstack-ready.sh`),
so consumers with different bucket lists share one container; only region drift
refuses reuse. A pre-existing container from an older launcher (stop-only,
bucket in its environment) is incompatible; remove it once with
`docker rm -f` and relaunch.

"Last consumer" is decided from the lease journal: each lease records its
process ID with a process identity, the host boot identity plus that process's
start time (`process_identity`). A lease whose recorded identity no longer
matches its process is stale, and its cleanup duty passes to a live consumer.
Linux uses `/proc/sys/kernel/random/boot_id`; macOS uses
`kern.bootsessionuuid` (`darwin:<uuid>:<start time>`). macOS `kern.boottime` is
not an identity: it is derived from the calendar clock, and its microseconds
move within one boot, which made live leases look stale and let another
consumer remove a container still in use. Leases recorded by earlier versions
carry that `kern.boottime` string; they still match their live process on the
boot second and start time, ignoring microseconds, while a different boot
second or start time still means a dead or reused process. A process identity
that cannot be read refuses cleanup instead of meaning stale. Restart a
long-running Run local after upgrading: an older running version compares
identities exactly and cannot recognize the new form.

Where it starts, and only there: recorded integration/E2E runs
(`RecordingPrerequisites`), Allure viewer/restore, Run local for a pack that
declares the `S3_APPLICATION_BUCKET` setting (its prerequisites stage leases it
beside the operation's own manager; other packs run without Docker for S3), and unit runs only when a
selected test carries a marker mapped to the built-in provider
(`localstack = "api_agent:shared_s3"` under `[tests.prerequisites]`; see
[resource packs](resource-packs.md#test-prerequisites)). Run local exports the
running endpoint to the agent process environment as `S3_ENDPOINT` and
`S3_REGION`, plus `S3_APPLICATION_BUCKET` (first `S3_EXTRA_BUCKETS` entry,
unless already set), and restores the previous values at shutdown. Tracing/API
plans never include LocalStack.

## Managed network cleanup

The launcher creates missing managed default networks with an explicit cleanup
ownership marker and Compose project/network labels before starting Compose.
It verifies existing networks' Compose ownership and leaves their labels and
configuration unchanged; this avoids a network migration on upgrade.
While holding the stack lease lock, ordinary release and
failed-start rollback prune only unused networks carrying that marker and the
current scope encoded in a single cleanup label. A container that still exists, including a stopped one
left by a crash, keeps its network; a released stack has no containers left, so its network goes with
them. Named volumes are always preserved.

Before acquiring a managed stack, the launcher also prunes unused networks with
the cleanup marker that were created more than 24 hours ago. This reclaims
orphaned networks from abandoned or relocated checkouts while allowing concurrent
Compose startups time to attach their containers. Network creation and cleanup
also share a per-user lock across checkouts and engine aliases, preventing cleanup from racing a
managed startup that is reattaching an older empty network. Cleanup uses the selected
engine/context and its native network-prune operation; it never runs system or
volume pruning. Unrelated networks and older networks without the explicit
cleanup marker are left alone, regardless of their names.

If older unlabelled networks exhaust the engine's address pools, inspect them
with `docker network ls`. A one-time `docker network prune` removes all networks
unused by any container, including manually created unused networks; review its
confirmation before accepting. The launcher does not perform that broad cleanup.

## Managed Compose project names

Each managed stack runs under a readable Compose project named after its checkout:
`sa-<checkout slug>-<first 8 hex of the scope id>`. The slug is the lowercase form of the
`api-agent.checkout` label (the pack checkout directory name, restricted to `[a-z0-9_-]` and at
most 40 characters), so volumes, networks and containers read like `sa-demo-pack-1a2b3c4d_jaeger-badger`
in a container UI, and two checkouts with the same directory name still get different projects. The
shared reporting stack is `sa-reporting-<first 8 hex of its binding key>`; its key is per engine and
endpoint, so a second reporting port gets its own project. Service names inside a project are unchanged.

The 16-hex scope id (`api-agent.scope` label, ownership labels on volumes and networks, journal and lock
file names) remains the identity. A project name is never trusted on its own: a container or network is owned
only when its `com.docker.compose.project` label is either the legacy `sa-<16 hex scope id>` or exactly the
name derived from the `api-agent.scope` and `api-agent.checkout` labels on that same object
(`ownership.accepted_projects`). That check applies wherever a name is verified: container ownership
(`StackScope.owns`), Compose planning, network pruning and the leaderless-container cleanup. Stacks created
under the legacy name stay recognised, reapable and never adopted by a new acquire, which only looks under
the derived name; they are not migrated. An installation enrolled with `enroll_reporting` records which of the
two names it was verified under and keeps using it.

**Upgrading is a breaking change for stored data.** A volume is named `<compose project>_<volume>`, so the
first acquire after upgrading creates new, empty volumes under the derived project name. Traces in Langfuse and
Jaeger and reports in LocalStack that were stored under the legacy `sa-<16 hex scope id>` project are no
longer shown, and the container management screen lists those old volumes as unused. They are not deleted by
the upgrade: the [unused-volume rule](#unused-managed-volumes) reclaims them after the retention period, or
earlier with Remove selected volumes. To keep old data, copy it into the new volume before then.

## Managed container lifecycle

Managed containers are disposable and their storage is not. A graceful release leaves no
managed container behind: it stops the containers its lease relied on and removes them by
immutable ID. Named volumes (`<compose project>_<volume>`, for example the Jaeger Badger store or
LocalStack data) are never removed by a release, and removal never passes `-v` or `-f`. The next acquire
recreates the container with the same Compose project, so Compose re-attaches the same named
volume. A container that is somehow still running after the stop makes the release fail with
its lease intact rather than being killed.

Where this happens:

- **Release** (`PrerequisiteManager.release`, which every Run local, test run and trace viewer
  ends with): the lease's containers (those it created and those it adopted), except any still
  protected by another live lease in the same journal, are stopped in dependency order and
  removed, then the scope's unreferenced network is pruned. An explicit `retain=True` keeps
  everything running. Once no container of the scope remains, the retained project snapshot
  directories (`<scope>-assets-*`) beside the journal are deleted too, since nothing can mount them.
- **Failed start**: rollback removes the containers it created or restarted the same way.
- **Shared reporting stack** (`SharedS3Manager.release`): when the last lease ends, its
  LocalStack container is removed and the data volume stays; the next acquire re-provisions the
  canonical binding over the same volume. A binding enrolled with `enroll_reporting` keeps its
  stopped container, because the binding pins the exact container it was verified against.
- **Acquire** (`PrerequisiteManager.acquire`): a leaderless-container cleanup runs while the
  scope lock is held, after the scope's own fail-closed checks and before any service is
  created. The caller's own scope keeps the ordinary acquire logic, which may adopt a running
  container of the same fingerprint that nothing leases (a crash leftover) or restart a stopped
  one; either way that container is removed at release.

Crash recovery. A killed or crashed process never reaches release. Every managed acquire and
release therefore also removes **leaderless** managed containers of any other scope: those whose
scope has no live lease in its own ownership journal. Running ones are stopped (5 second grace)
and then removed; stopped ones are removed; volumes are left to the [volume rule](#unused-managed-volumes). The journal is found beside the
`com.docker.compose.project.working_dir` snapshot directory the container recorded, and is read
through the same lock, identity and lease checks that guard stop (`guarded_cleanup`). A scope is
preserved, untouched and unstopped, when:

- any live lease from another process protects it,
- its journal is unreadable or its lock is held (an acquisition or release is in progress),
- its containers record an unrecognised layout (different or relative directories, a directory
  that is not this scope's snapshot, or no recorded directory),
- a container carries ownership labels that do not belong together (its project label must be the legacy
  or derived name its own scope and checkout labels reproduce), or a container is already being removed.

A scope whose checkout directory is provably gone (the filesystem reports it absent, and host paths are
visible) has no journal and therefore no lease, so its leftovers are removed. A journal directory that
cannot be read, or that this process cannot see, preserves the scope. A container that an operator-enrolled
reporting binding pins (`enroll_reporting`) is never removed, and an unreadable registry preserves every container. When nothing is leaderless the cost is one label-filtered listing (`ps -a`) and one batched
inspection. The cleanup never fails a run: it has its own 30 second budget inside the
manager's deadline (so a slow engine can spend up to that, plus a one second lock wait per contended scope, of
the acquire's own deadline while the scope lock is held), honors cancellation, preserves everything it cannot
verify, and logs only a short value-free diagnostic. After removing a scope's containers it prunes that scope's
cleanup-marked network when no saved container references it. Removing containers never removes a
volume, and the cleanup never removes images or build cache or uses `-f`, `-v` or system pruning.

Managed containers and networks carry `api-agent.checkout=<checkout directory name>`, which the
[project name](#managed-compose-project-names) is derived from. Containers without it, created
before, keep working under the legacy name.

The caller's own scope is not exempt from this cleanup for containers under the legacy project name: its
current stack keeps the ordinary acquire logic, but a leftover of the same scope under the old name that no live
lease protects is removed too (under the lock the acquire already holds, or under the usual guard at release).

## Unused managed volumes

Removing a container never removes its volume, so named volumes accumulate until a separate rule
reclaims them. The same acquire/release pass that cleans leaderless containers also removes a managed
volume when **all** of these hold:

- it carries the full ownership labels (`api-agent.managed=true`, a 16-hex `api-agent.scope`,
  an `api-agent.profile`) and a `com.docker.compose.project` label that is the legacy name or a derived
  name embedding that same scope's 8-hex prefix, and its name is `<project>_<volume>`
  (volumes carry no checkout label, so the slug itself cannot be recomputed);
- **no container references it**, running or stopped (`docker ps -a --filter volume=<name>`), checked
  again after the scope's lock is held; the engine independently refuses to remove a mounted volume;
- its scope is not the caller's own or a previous path-based scope and holds **no live lease** in any of
  its recorded journal directories (read under the same lock and process-identity checks as stop);
- its scope is **orphaned** (every recorded journal directory is gone, which needs host paths to be
  visible: a local `unix://` engine and no containerised session) and was last acquired at least
  `MANAGED_CONTAINER_RETENTION_DAYS` days ago, or **idle**: its journal was last written at least that many days
  ago (default 14). With `0`, the idle rule is off and an orphan goes immediately. Journals live under the
  git-ignored `artifacts/`, which `git clean -fdx` or an unmounted drive can remove while the checkout is still
  in use, so an orphan waits out the retention period as well.

Docker volumes carry no last-use time, and their creation date is not their last use. The modification time
of the scope's journal file, rewritten by every acquire and release, is the proxy; for an orphan, the
modification time of its scope record (refreshed by every acquire) is. A directory that still
exists without its journal, an unreadable journal, an unverifiable absence, or an unknown location all preserve
the volumes. Removal is batched, bounded by the same 30 second budget, honours cancellation, never uses `-f`,
and never removes images, build cache or an unowned volume.

A named volume cannot say which checkout used it, and its containers are gone, so each acquire records the journal
directory of its scope in a small private file under the user cache (`~/.cache/api-agent/scopes/<scope id>.json`,
at most 16 directories, `XDG_CACHE_HOME` honoured), and the container cleanup records the location it
learns from a container it removes. The file is forgotten when an orphaned scope's last volume goes. A scope
with no record, such as volumes whose stack was never seen by this version, is preserved.

**Container management** (`TUI_MODE=cleanup-containers`) lists every owned managed volume after the containers
(name, project and profile, in use or unused). Enter marks unused volume rows; **Remove selected volumes** asks for a
literal `y`, then applies the same verification (labels, no container reference, no live lease, known location) without the
idle and orphan rules, and says how many were removed and why the rest were kept. **Remove unused** runs the
whole cleanup (`ContainerRuntime.reap_unused`) with the configured retention and reports containers, volumes
and networks. **Stop** only stops; the stopped containers are removed by the next release or cleanup.

## Other conditional requirements

Other downloadable tools are prepared at their existing operation boundaries,
including browsers, mkcert/OpenSSL for local HTTPS, Java/Allure for the report viewer, and Java plus the
pinned PlantUML renderer JAR (a lazy mise tool) for live workflow views. Host browser
libraries, a graphical session, certificate-trust approval, credentials,
and service access may need your attention when relevant; the app reports these
requirements where they are used.

See [setup and configuration](workbench.md#getting-started),
[tracing](tracing.md) and [development](development.md) for operation-specific setup.

Callers that create temporary prerequisite definitions may opt in with
`PrerequisiteManager(..., dispose_on_release=True)`; the shared S3 manager always does. Every other
caller's containers are removed at release by the [managed container lifecycle](#managed-container-lifecycle);
the temporary mode keeps its stricter nonce-verified ownership and does not extend to containers it merely
adopted (an enrolled shared S3 binding stays stop-only). Temporary ownership follows compatible borrowers until their final
release; conflicting policies for the same definition are rejected. Cleanup
verifies immutable container IDs, creator nonces and configuration before removing definitions,
and removes a network created by that lifecycle only when no saved container
references it. The creator nonce follows each cleanup duty across borrowers;
a compatible definition appearing during startup without that evidence is not
adopted for disposal. Definitions predating the temporary lifecycle, volumes,
images and build caches remain.

When native acquisition is supervised, its private recovery intent carries the
same policy and an acquisition nonce. A confirmed-dead worker's temporary
containers are checked against that nonce, their recorded configuration and the
active lease journal before removal. Newly created networks carry the nonce too;
recovery removes only a matching managed network after checking all saved
container references. Older intents retain stop-only behavior.
