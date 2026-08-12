#!/usr/bin/env bash
# Instala graft-mem-vault para jcode.
#
# jcode no inyecta contexto desde hooks, así que el puente escribe
# .jcode/prompt-overlay.md, que jcode sí monta en el prompt.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JCODE_HOME="${JCODE_HOME:-$HOME/.jcode}"
PACKAGE="$JCODE_HOME/graft-mem-vault"
CONFIG="$JCODE_HOME/config.toml"
BRIDGE="$PACKAGE/hooks/jcode-vault-bridge.py"
JCODE_HOOKS="$JCODE_HOME/hooks"
GATE="$JCODE_HOOKS/jcode-tldr-rtk-gate.py"

ok()    { echo "✓ $1"; }
falta() { echo "✗ $1" >&2; }

mkdir -p "$PACKAGE/scripts" "$PACKAGE/hooks" "$JCODE_HOOKS"

cp "$DIR/graft_mem_vault.py" "$DIR/refresh_vaults.py" \
   "$DIR/vault_lookup.py" "$DIR/vault_scout.py" "$PACKAGE/scripts/"
cp "$DIR/hooks/vault-session-start.py" \
   "$DIR/hooks/jcode-vault-bridge.py" "$PACKAGE/hooks/"
cp "$DIR/hooks/jcode-tldr-rtk-gate.py" "$GATE"
chmod +x "$PACKAGE/hooks/jcode-vault-bridge.py" "$GATE"
ok "scripts y hooks en $PACKAGE"

if [ ! -f "$CONFIG" ]; then
    printf '[hooks]\n' > "$CONFIG"
    ok "config.toml creado"
fi

python3 - "$CONFIG" "$BRIDGE" "$GATE" <<'PY'
import sys

config_path, bridge, gate = sys.argv[1], sys.argv[2], sys.argv[3]
wanted = {
    "session_start": f'"{bridge} --context"',
    "session_end":   f'"{bridge} --summarize"',
    "post_tool":     f'"{bridge} --observation"',
    "turn_end":      f'"{bridge} --turn-end"',
    "pre_tool":      f'"{gate}"',
    "pre_tool_timeout_ms": "5000",
}

with open(config_path) as handle:
    lines = handle.readlines()

# Localizar la sección [hooks] y su final.
start = None
for index, line in enumerate(lines):
    if line.strip() == "[hooks]":
        start = index
        break

if start is None:
    lines.append("\n[hooks]\n")
    start = len(lines) - 1
    end = len(lines)
else:
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("["):
            end = index
            break

block = lines[start + 1:end]
existing = {
    line.split("=", 1)[0].strip()
    for line in block
    if "=" in line and not line.strip().startswith("#")
}

added = [
    f"{key} = {value}\n"
    for key, value in wanted.items()
    if key not in existing
]

if added:
    # Insertar justo tras las claves ya presentes, antes de la línea en blanco
    # final, para no romper la separación entre secciones.
    tail = len(block)
    while tail > 0 and not block[tail - 1].strip():
        tail -= 1
    new_block = block[:tail] + added + block[tail:]
    lines[start + 1:end] = new_block
    with open(config_path, "w") as handle:
        handle.writelines(lines)
    print(f"✓ {len(added)} hook(s) cableados en [hooks]")
else:
    print("✓ hooks ya cableados (sin cambios)")
PY

ok "instalación completa"
echo
echo "Recomendado: conserva un wrapper de shell que ejecute --context antes"
echo "de arrancar jcode para evitar la carrera del primer prompt."
