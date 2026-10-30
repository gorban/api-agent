#!/bin/sh
# Supply project tools, then enter the existing Python launcher.
set -eu
if ! command -v mise >/dev/null 2>&1; then
  printf '%s\n' 'mise is required. Follow https://mise.jdx.dev/installing-mise.html, load it in this terminal, then retry.' >&2
  exit 2
fi
if ! mise --version >/dev/null 2>&1; then
  printf '%s\n' 'mise could not run. Check your mise installation at https://mise.jdx.dev/installing-mise.html, then retry.' >&2
  exit 2
fi
entry=$0
while [ -L "$entry" ]; do
  directory=$(CDPATH= cd -P "$(dirname "$entry")" && pwd)
  target=$(readlink "$entry")
  case "$target" in /*) entry=$target ;; *) entry=$directory/$target ;; esac
done
root=$(CDPATH= cd -P "$(dirname "$entry")" && pwd)
state="$root/.api-agent/bootstrap"
export API_AGENT_MISE=1 API_AGENT_CALLER_CWD="$(pwd -P)"
# Keep project tool selection separate from shell configuration and other repos.
export MISE_CONFIG_DIR="$state/mise/config" MISE_GLOBAL_CONFIG_FILE="$state/mise/config/config.toml"
export MISE_SYSTEM_CONFIG_DIR="$state/mise/system" MISE_SYSTEM_CONFIG_FILE="$state/mise/system/config.toml"
export MISE_DATA_DIR="$state/mise/data" MISE_CACHE_DIR="$state/mise/cache" MISE_STATE_DIR="$state/mise/state"
export MISE_CEILING_PATHS="$(dirname "$root")" MISE_OVERRIDE_CONFIG_FILENAMES=mise.toml
export MISE_ENV= MISE_NO_ENV=1 MISE_NO_HOOKS=1
exec mise --cd "$root" --no-env --no-hooks exec core:python aqua:astral-sh/uv -- \
  python "$root/src/api_agent/runtime/entry.py" "$@"
