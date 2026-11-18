# Docker and optional prerequisites

Follow [Get started](../README.md#get-started) to install mise, the always-required
tool prerequisite. Additional requirements depend on the operation you select.

## When Docker is needed

Docker is an optional development dependency. Install and start an accessible
supported engine, with its client and Compose support, when selecting any of these:

- Managed local Jaeger or LangFuse tracing, including their backing services.
- Managed LocalStack report storage.
- A containerized local API and its dependencies.
- A provider, workflow tool, or test that requires containers.
- Container image builds or container-based tests.

External tracing, external S3 storage, and an external API each remove the need
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

## Managed network cleanup

The launcher creates missing managed default networks with an explicit cleanup
ownership marker and Compose project/network labels before starting Compose.
It verifies existing networks' Compose ownership and leaves their labels and
configuration unchanged; this avoids a network migration on upgrade.
While holding the stack lease lock, ordinary release and
failed-start rollback prune only unused networks carrying that marker and the
current scope encoded in a single cleanup label. Retained containers, including stopped containers, keep
their networks. Named volumes and container definitions are preserved.

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

## Unused managed resource reaping

Release stops containers and keeps them, with their volumes and network, so the
current checkout reuses its stack quickly. Scopes are per checkout, so a deleted
checkout or an abandoned scope would otherwise leave stopped containers, volumes
and networks behind forever. The stop-only reuse therefore applies to the current
checkout; other checkouts' unused stacks are reaped automatically.

`PrerequisiteManager.acquire` (every Run local, test run and trace viewer start)
reaps while holding its own scope lock, after that scope's fail-closed checks and before it
creates any service. `PrerequisiteManager.release` reaps once the lease has ended. Release
skips it after a failed release or when cancelled. When nothing is unused the cost is one
label-filtered listing (`ps -a`) and one batched inspection; an idle engine
costs only the listing. The reaper never fails or delays a run: it has its own bounded budget inside
the manager's deadline, honors cancellation, preserves everything it cannot verify (the
same posture as network pruning), and logs only a short value-free diagnostic.

A scope is a candidate only when every one of its `api-agent.managed` containers
is stopped (`exited`, `created` or `dead`; paused, restarting or removing counts as
live), it is not the caller's scope or a previous path-based scope, and no live lease
protects it in its own ownership journal. The journal is found beside the recorded
`com.docker.compose.project.working_dir` snapshot directory and read through the
same lock, identity and lease checks that guard stop (`guarded_cleanup`); an
unreadable journal, a held lock or an unrecognised directory layout preserves the
scope. A candidate is reaped when either rule holds:

- **Orphan:** the recorded working directory no longer exists on this host. This rule always
  applies, but only when this process can see the engine host's paths (a
  local `unix://` engine and no containerised session); it is skipped for remote engines
  and sessions inside a container.
- **Aged:** every container has been stopped for at least
  `MANAGED_CONTAINER_RETENTION_DAYS` (default 14; `0` disables only this age rule).
  Age counts from the most recent stop, or creation for a never-started container.

Reaping removes, in batches, the scope's containers; then volumes that carry the same
compose project label and the full api-agent ownership labels (managed, scope and
profile); then the scope's cleanup-marked network when no saved container
references it. It never removes images, build cache, anonymous volumes, or anything
without the api-agent ownership labels, and it never uses `-f`, `-v` or system/volume
pruning. Stack volumes hold local trace and result history, which is deleted with an
orphaned or aged stack; a shared reporting stack is re-provisioned the next time it is needed.

Managed containers also carry `api-agent.checkout=<checkout directory name>` (label
only; project and container names are unchanged) so a container UI can tell scopes
apart. Existing containers without it keep working.

For a one-time cleanup, **Container management** (`TUI_MODE=cleanup-containers`) offers
**Remove unused stopped stacks**, which runs the same reaper
(`ContainerRuntime.reap_unused`, built on `reap_unused_managed_resources`) with the
configured retention and reports what was removed.

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
`PrerequisiteManager(..., dispose_on_release=True)`. The default remains
stop-only (with [unused-resource reaping](#unused-managed-resource-reaping) for other scopes). Temporary ownership follows compatible borrowers until their final
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
