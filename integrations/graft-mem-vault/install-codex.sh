#!/usr/bin/env bash
# Instala graft-mem-vault y la ingesta nativa de claude-mem para Codex.
#
#   bash install-codex.sh              instala o actualiza
#   bash install-codex.sh --check      solo comprueba dependencias
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
PACKAGE="$CODEX_DIR/graft-mem-vault"
SCRIPTS="$PACKAGE/scripts"
HOOKS="$PACKAGE/hooks"
SETTINGS="$CODEX_DIR/hooks.json"
MEM_DB="${CLAUDE_MEM_DB:-$HOME/.claude-mem/claude-mem.db}"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

SOLO_CHECK=0
for a in "$@"; do
    [ "$a" = "--check" ] && SOLO_CHECK=1
done

fallos=0
ok()    { printf '  ✓ %s\n' "$1"; }
falta() { printf '  ✗ %s\n' "$1"; fallos=$((fallos+1)); }

echo "Dependencias de graft-mem-vault para Codex:"
if command -v python3 >/dev/null 2>&1; then
    ok "python3"
else
    falta "python3 no encontrado"
fi

if python3 - <<'PY' 2>/dev/null
import sqlite3
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
PY
then
    ok "sqlite3 de Python con FTS5"
else
    falta "el sqlite3 de Python no trae FTS5"
fi

if command -v git >/dev/null 2>&1; then
    ok "git"
else
    falta "git no encontrado"
fi

if command -v graft >/dev/null 2>&1; then
    ok "graft"
else
    falta "graft no encontrado"
fi

if [ -f "$MEM_DB" ]; then
    ok "claude-mem en $MEM_DB"
else
    falta "no hay base de claude-mem en $MEM_DB (CLAUDE_MEM_DB para cambiarla)"
fi

MEM_HOOKS="${CLAUDE_MEM_CODEX_HOOKS:-}"
if [ -z "$MEM_HOOKS" ]; then
    MEM_HOOKS="$(python3 - "$CLAUDE_DIR" "$CODEX_DIR" <<'PY'
import glob
import os
import sys

claude_dir, codex_dir = map(os.path.expanduser, sys.argv[1:])

for root in (os.environ.get("CLAUDE_PLUGIN_ROOT"), os.environ.get("PLUGIN_ROOT")):
    if not root:
        continue
    for candidate in (os.path.join(root, "hooks", "codex-hooks.json"),
                      os.path.join(root, "plugin", "hooks", "codex-hooks.json")):
        if os.path.isfile(candidate):
            print(candidate)
            raise SystemExit


def version_key(path):
    version = os.path.basename(os.path.dirname(os.path.dirname(path)))
    numbers = version.split("-", 1)[0].split(".")
    parsed = tuple(int(n) if n.isdigit() else 0 for n in (numbers + ["0"] * 3)[:3])
    return parsed + ("-" not in version, version)


cached = []
for pattern in (
    os.path.join(claude_dir, "plugins", "cache", "thedotmack", "claude-mem", "*",
                 "hooks", "codex-hooks.json"),
    os.path.join(codex_dir, "plugins", "cache", "*", "claude-mem", "*",
                 "hooks", "codex-hooks.json"),
):
    cached.extend(
        path for path in glob.glob(pattern)
        if not os.path.exists(os.path.join(
            os.path.dirname(os.path.dirname(path)), ".orphaned_at"))
    )
if cached:
    print(max(cached, key=version_key))
    raise SystemExit

marketplace = os.path.join(
    claude_dir, "plugins", "marketplaces", "thedotmack", "plugin", "hooks",
    "codex-hooks.json")
if os.path.isfile(marketplace):
    print(marketplace)
PY
)"
fi
if [ -n "$MEM_HOOKS" ] && [ -f "$MEM_HOOKS" ]; then
    ok "hooks nativos de claude-mem para Codex"
else
    falta "claude-mem no aporta hooks/codex-hooks.json (CLAUDE_MEM_CODEX_HOOKS para indicarlo)"
fi

if [ "$fallos" -gt 0 ]; then
    echo "✗ faltan $fallos dependencias — no instalo nada"
    exit 1
fi
if [ "$SOLO_CHECK" -eq 1 ]; then
    echo "(--check: no se ha instalado nada)"
    exit 0
fi

mkdir -p "$SCRIPTS" "$HOOKS"
cp "$DIR/graft_mem_vault.py" "$DIR/refresh_vaults.py" "$DIR/vault_lookup.py" "$SCRIPTS/"
cp "$DIR/hooks/vault-session-start.py" "$DIR/hooks/vault-read-nudge.py" \
   "$DIR/hooks/vault-prompt-search.py" "$HOOKS/"
chmod +x "$SCRIPTS"/*.py "$HOOKS"/*.py
echo "✓ scripts y hooks en $PACKAGE"

python3 - "$SETTINGS" "$PACKAGE" "$MEM_HOOKS" <<'PY'
import copy
import json
import os
import shlex
import shutil
import sys
import time

settings, package, mem_hooks_path = sys.argv[1:]
if os.path.exists(settings):
    with open(settings) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"{settings} no contiene un objeto JSON")
    shutil.copy2(settings, f"{settings}.bak-{int(time.time())}")
else:
    data = {}

hooks = data.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"{settings}: hooks no es un objeto JSON")

with open(mem_hooks_path) as fh:
    native_data = json.load(fh)
native_hooks = native_data.get("hooks")
if not isinstance(native_hooks, dict):
    raise SystemExit(f"{mem_hooks_path}: hooks no es un objeto JSON")
plugin_root = os.path.dirname(os.path.dirname(mem_hooks_path))
context_runner = os.path.join(plugin_root, "scripts", "bun-runner.js")
context_worker = os.path.join(plugin_root, "scripts", "worker-service.cjs")
if not os.path.isfile(context_runner) or not os.path.isfile(context_worker):
    raise SystemExit(f"{plugin_root}: faltan los scripts del worker de claude-mem")


def is_native_codex(group):
    for handler in group.get("hooks", []):
        command = " ".join(
            str(handler.get(key, "")) for key in ("command", "commandWindows")
        )
        if "CLAUDE_MEM_CODEX_HOOK" in command and " hook codex " in command:
            return True
    return False


native_added = 0
for event, groups in native_hooks.items():
    if not isinstance(groups, list):
        raise SystemExit(f"{mem_hooks_path}: {event} no es una lista")
    current = hooks.setdefault(event, [])
    hooks[event] = [group for group in current if not is_native_codex(group)]
    for source in groups:
        group = copy.deepcopy(source)
        if event == "SessionStart":
            group["matcher"] = "startup|resume|compact|clear"
            for handler in group.get("hooks", []):
                if " hook codex context" not in handler.get("command", ""):
                    continue
                handler["command"] = (
                    "CLAUDE_MEM_CODEX_HOOK=1 node "
                    f"{shlex.quote(context_runner)} {shlex.quote(context_worker)} "
                    "hook codex context"
                )
                handler.pop("commandWindows", None)
        hooks[event].append(group)
        native_added += 1

specs = [
    ("SessionStart", "startup|resume|compact|clear", "vault-session-start.py"),
    ("UserPromptSubmit", None, "vault-prompt-search.py"),
    ("PreToolUse", "apply_patch|Edit|Write", "vault-read-nudge.py"),
    ("PostToolUse", "mcp__codebase_memory_mcp__get_code_snippet", "vault-read-nudge.py"),
]
added = 0
for event, matcher, filename in specs:
    script = os.path.join(package, "hooks", filename)
    if any(script in hook.get("command", "")
           for group in hooks.get(event, [])
           for hook in group.get("hooks", [])):
        continue
    command = (
        f"GRAFT_MEM_VAULT_HOME={shlex.quote(package)} "
        f"python3 {shlex.quote(script)}"
    )
    group = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
    if matcher:
        group["matcher"] = matcher
    hooks.setdefault(event, []).append(group)
    added += 1

with open(settings, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(f"✓ {native_added} hooks nativos de claude-mem registrados")
print(f"✓ {added} hooks nuevos de graft-mem-vault en {settings}")
PY

echo
echo "Hooks instalados. En Codex revisa /hooks para aprobarlos si solicita confianza."
echo "claude-mem observará Codex; el SessionStart inyecta memoria y refresca el vault en segundo plano."
