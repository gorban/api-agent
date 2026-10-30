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

These options do not require Docker Desktop. Container-registry accounts,
download limits and hosted services have separate terms from the engine.

### Using Podman with Docker commands

The launcher invokes a `docker` executable, including `docker compose`. Podman
can provide that interface. Follow its
[Docker CLI compatibility guide](https://podman-desktop.io/docs/migrating-from-docker/aliasing-the-docker-cli)
and [Compose setup](https://podman-desktop.io/docs/compose/setting-up-compose).
Compose is a separate provider; installing Podman alone does not install it.

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
Docker executable. This wrapper forwards arguments unchanged; `docker compose`
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
env docker compose version
```

Using `env` bypasses shell aliases and checks the executable the launcher can
actually invoke. Both commands should succeed without `sudo`; a missing Compose
provider or inaccessible engine needs fixing before selecting container features.
Shared local report infrastructure additionally requires a local Unix socket,
not an arbitrary remote engine endpoint.

## Other conditional requirements

Other downloadable tools are prepared at their existing operation boundaries,
including browsers, mkcert, and Java/Allure for the report viewer. Host browser
libraries, a graphical session, OpenSSL, certificate-trust approval, credentials,
and service access may need your attention when relevant; the app reports these
requirements where they are used.

See [setup and configuration](workbench.md#getting-started),
[tracing](tracing.md) and [development](development.md) for operation-specific setup.
