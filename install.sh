#!/usr/bin/env bash
# Installs (or updates) the gpu-gate plugin into a Hermes profile and enables it.
#
#   ./install.sh                 current profile (~/.hermes, or $HERMES_HOME)
#   ./install.sh tresorier       profile ~/.hermes/profiles/tresorier
#   HERMES_BIN=/path/to/hermes ./install.sh     which hermes binary to use
#
# Idempotent: re-running overwrites the plugin files and leaves the settings
# (plugins.entries.gpu-gate.settings) untouched. Restarting the gateway / the
# desktop is required for the middleware to be reloaded.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${1:-}"
HERMES_BIN="${HERMES_BIN:-hermes}"

if [[ -n "$PROFILE" ]]; then
  HOME_DIR="$HOME/.hermes/profiles/$PROFILE"
  ARGS=(-p "$PROFILE")
else
  HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
  ARGS=()
fi

[[ -d "$HOME_DIR" ]] || { echo "profile not found: $HOME_DIR" >&2; exit 1; }
command -v "$HERMES_BIN" >/dev/null || { echo "'$HERMES_BIN' not found (HERMES_BIN=...)" >&2; exit 1; }

DEST="$HOME_DIR/plugins/gpu-gate"
echo "== copying into $DEST"
mkdir -p "$DEST"
install -m 0644 "$HERE/plugin.yaml" "$HERE/__init__.py" "$HERE/gate.py" "$DEST/"

echo "== validating the manifest"
"$HERMES_BIN" plugins validate "$DEST" >/dev/null && echo "  manifest OK"

echo "== enabling"
"$HERMES_BIN" "${ARGS[@]}" plugins enable gpu-gate

cat <<EOF

gpu-gate is installed and enabled for $(basename "$HOME_DIR").

Next steps:
  1. restart the gateway / the desktop (the middleware is loaded at startup)
  2. check     : hermes ${PROFILE:+-p $PROFILE }plugins list | grep gpu-gate
                 hermes ${PROFILE:+-p $PROFILE }gpu-prio status
  3. in a session: /gpu-prio low   or   /gpu-prio ultralow
  4. settings  : plugins.entries.gpu-gate.settings.{verdict_url,engine_prefixes,
                 idle_min_s,max_wait_s,max_wait_unattended_s,default_mode,
                 unattended_mode,mode_by_platform}
                 (see README.md — a restart is needed after a change)

By default an unattended session (cron, 'chat -q', detached worker) is already
treated as "low" without any configuration; an interactive session stays "normal".
EOF
