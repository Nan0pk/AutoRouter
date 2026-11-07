#!/usr/bin/env bash
# Install the AutoRouter OpenCode example into a user's OpenCode config directory.
#
#   bash scripts/install_opencode.sh                 # install into ~/.config/opencode
#   bash scripts/install_opencode.sh --dry-run       # show what would change
#   bash scripts/install_opencode.sh --config-dir DIR
#
# What it does:
#   1. backs up any file it is about to overwrite   (DIR/.backup-YYYYmmdd-HHMMSS/)
#   2. copies examples/opencode/{opencode.jsonc,agents/*.md,plugins/auto-model-reset/}
#   3. installs the neutral `eval` agent used by eval/adapters/opencode-adapter.py
#   4. validates the installed config (and against the live model catalog when
#      the opencode CLI is available)
#
# What it does NOT do: touch auth/credentials, remove models you added, or
# restart OpenCode for you. It also never writes a provider API key — the
# example config authenticates through `opencode auth`.
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="$(pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
DRY_RUN=0
EVAL_AGENT=1
STAMP="$(date +%Y%m%d-%H%M%S)"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Flags:
  --config-dir DIR   target config directory (default: ${XDG_CONFIG_HOME:-~/.config}/opencode)
  --dry-run          print the plan, change nothing
  --no-eval-agent    skip installing agents/eval.md
  -h, --help         this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config-dir) CONFIG_DIR="${2:?--config-dir needs a value}"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --no-eval-agent) EVAL_AGENT=0; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

SRC="$REPO/examples/opencode"
[ -f "$SRC/opencode.jsonc" ] || { echo "ERROR: $SRC/opencode.jsonc not found" >&2; exit 1; }

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/opencode"

# The example config carries placeholders so the repo stays relocatable:
#   __AUTOROUTER_REPO__   -> this checkout (gate-router needs classify.py)
#   __AUTOROUTER_STATE__  -> the ledger directory
render() {
  sed -e "s|__AUTOROUTER_REPO__|$REPO|g" \
      -e "s|__AUTOROUTER_STATE__|$STATE_DIR|g" "$1"
}

say() { printf '%s\n' "$*"; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  would: %s\n' "$*"
  else
    "$@"
  fi
}

# Files to install: "source relative to repo" -> "destination relative to config dir"
PLAN=(
  "examples/opencode/opencode.jsonc|opencode.jsonc"
)
for agent in "$SRC"/agents/*.md; do
  PLAN+=("${agent#$REPO/}|agents/$(basename "$agent")")
done
if [ "$EVAL_AGENT" -eq 1 ]; then
  PLAN+=("eval/adapters/opencode-agent.md|agents/eval.md")
fi

say "AutoRouter -> OpenCode install"
say "  repo:   $REPO"
say "  config: $CONFIG_DIR"
[ "$DRY_RUN" -eq 1 ] && say "  mode:   DRY RUN (nothing will be written)"
say ""

changed=()
for entry in "${PLAN[@]}"; do
  src="$REPO/${entry%%|*}"
  dst="$CONFIG_DIR/${entry##*|}"
  [ -f "$src" ] || { echo "ERROR: missing source file: $src" >&2; exit 1; }
  if [ -f "$dst" ] && cmp -s <(render "$src") "$dst"; then
    continue
  fi
  changed+=("$entry")
done

PLUGINS=(auto-model-reset gate-router)
plugin_changed=()
for name in "${PLUGINS[@]}"; do
  if [ -d "$CONFIG_DIR/plugins/$name" ] && diff -rq "$SRC/plugins/$name" "$CONFIG_DIR/plugins/$name" >/dev/null 2>&1; then
    continue
  fi
  plugin_changed+=("$name")
done

if [ "${#changed[@]}" -eq 0 ] && [ "${#plugin_changed[@]}" -eq 0 ]; then
  say "Already up to date. Nothing to do."
  exit 0
fi

if [ "${#changed[@]}" -gt 0 ]; then
  say "Files:"
  for entry in "${changed[@]}"; do
    dst="$CONFIG_DIR/${entry##*|}"
    if [ -f "$dst" ]; then
      say "  update ${entry##*|} (backup kept)"
    else
      say "  create ${entry##*|}"
    fi
  done
fi
for name in "${plugin_changed[@]}"; do
  say "Plugin: plugins/$name (copy/update)"
done
say ""

BACKUP="$CONFIG_DIR/.backup-$STAMP"
BACKUP_MADE=0
if [ "${#changed[@]}" -gt 0 ] || [ "${#plugin_changed[@]}" -gt 0 ]; then
  run mkdir -p "$CONFIG_DIR"
  needs_backup=0
  for entry in "${changed[@]}"; do
    [ -f "$CONFIG_DIR/${entry##*|}" ] && needs_backup=1
  done
  for name in "${plugin_changed[@]}"; do
    [ -d "$CONFIG_DIR/plugins/$name" ] && needs_backup=1
  done
  if [ "$needs_backup" -eq 1 ]; then
    run mkdir -p "$BACKUP"
    for entry in "${changed[@]}"; do
      rel="${entry##*|}"
      if [ -f "$CONFIG_DIR/$rel" ]; then
        run mkdir -p "$BACKUP/$(dirname "$rel")"
        run cp -p "$CONFIG_DIR/$rel" "$BACKUP/$rel"
      fi
    done
    for name in "${plugin_changed[@]}"; do
      if [ -d "$CONFIG_DIR/plugins/$name" ]; then
        run mkdir -p "$BACKUP/plugins"
        run cp -a "$CONFIG_DIR/plugins/$name" "$BACKUP/plugins/"
      fi
    done
    say "  backup: $BACKUP"
    BACKUP_MADE=1
  fi
fi

for entry in "${changed[@]}"; do
  src="$REPO/${entry%%|*}"
  rel="${entry##*|}"
  run mkdir -p "$CONFIG_DIR/$(dirname "$rel")"
  if [ "$DRY_RUN" -eq 1 ]; then
    run "render+write $src -> $CONFIG_DIR/$rel"
  else
    render "$src" > "$CONFIG_DIR/$rel"
  fi
done

for name in "${plugin_changed[@]}"; do
  run rm -rf "$CONFIG_DIR/plugins/$name"
  run mkdir -p "$CONFIG_DIR/plugins"
  run cp -a "$SRC/plugins/$name" "$CONFIG_DIR/plugins/$name"
done

say ""
say "Validating installed config"
if [ "$DRY_RUN" -eq 1 ]; then
  say "  skipped (dry run)"
else
  catalog="$(mktemp -t autorouter-catalog.XXXXXX.json)"
  if command -v opencode >/dev/null 2>&1; then
    opencode models 2>/dev/null | python3 -c '
import json, sys
rows = []
for line in sys.stdin:
    line = line.strip()
    if "/" not in line:
        continue
    provider, ident = line.split("/", 1)
    rows.append({"providerID": provider, "id": ident, "enabled": True,
                 "status": "active", "variants": []})
json.dump(rows, open(sys.argv[1], "w"))
' "$catalog" || rm -f "$catalog"
    if [ -s "$catalog" ]; then
      python3 scripts/validate_router.py --config "$CONFIG_DIR/opencode.jsonc" --catalog "$catalog"
    else
      python3 scripts/validate_router.py --config "$CONFIG_DIR/opencode.jsonc"
    fi
    rm -f "$catalog"
  else
    python3 scripts/validate_router.py --config "$CONFIG_DIR/opencode.jsonc"
  fi
fi

say ""
say "Done."
say "  restart the OpenCode server/TUI to load the config:"
say "    opencode reload   (or just restart your session)"
say "  then check the agents and the routing:"
say "    opencode debug agents"
if [ "$BACKUP_MADE" -eq 1 ]; then
  say "  rollback:"
  say "    cp -a $BACKUP/. $CONFIG_DIR/"
fi
say ""
say "Reminder: eval/adapters/opencode-adapter.py needs the \`eval\` agent, which this"
say "script installs unless you passed --no-eval-agent."
