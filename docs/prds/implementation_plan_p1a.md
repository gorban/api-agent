# Agreed getting-started Markdown

[First-run requirements](implementation_plan_p1.md) · [Mise startup](implementation_plan_p.md)

This is the delivered README acceptance baseline for the surgical migration. Keep it
aligned with README.md. The existing base Python environment is deliberately retained;
this is not a promise of minimal TUI-only dependencies or unexecuted platform support.

````markdown
## Get started

On Unix-like systems with a POSIX shell,
[mise](https://mise.jdx.dev/installing-mise.html) is always required.
Docker is a development dependency required
[only for certain local services](docs/prerequisites.md#when-docker-is-needed),
tools and tests. Mise supplies this
project's Python, uv, Node.js and npm; you do not need to install those separately.
Install mise with one of these official commands:

```sh
curl https://mise.run | sh
```

Or, with wget:

```sh
wget -qO- https://mise.run | sh
```

If neither command is available, open `https://mise.run` in a browser, save the
raw installer script, open a terminal in its download directory, and run it with
`sh`. Follow mise's [shell setup instructions](https://mise.jdx.dev/shell-setup.html)
for your shell's startup file, such as `.bashrc` or `.zshrc`, so mise is available
in future terminals. The installer cannot change the terminal that started it.
For its default installation, make mise available in this terminal now:

```sh
export PATH="$HOME/.local/bin:$PATH"
command -v mise
mise --version
```

For a custom installation location, use that location instead. Download and
extract the repository ZIP, or use a Git checkout. Open a terminal in that
repository directory, then run:

```sh
chmod +x launch.sh # if the downloaded ZIP lost executable permissions
./launch.sh
```

The launcher prepares Python/uv and its existing base Python environment,
then opens the terminal menu. Choose **Run local** to start the app. Later
launches reuse installed tools. Existing command-line options follow
`./launch.sh` unchanged. Initial preparation needs internet access and writable
user storage; a Git checkout is not required. Node/npm and other optional tools
are acquired when an operation needs them.

````

## Acceptance use

- [ ] Review basic-text readability and the official installer/shell/PATH sequence.
- [ ] Keep configuration and detailed prerequisites below or linked from this section.
- [ ] Record executed fresh-machine/platform paths separately from intended support.
