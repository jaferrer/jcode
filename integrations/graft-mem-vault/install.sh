#!/usr/bin/env bash
# Instala graft-mem-vault: scripts, regla global y hooks de sesion.
#
# Comprueba dependencias antes de tocar nada. Las obligatorias abortan; las
# opcionales solo avisan de que se pierde. Es idempotente: reejecutarlo no
# duplica hooks ni pisa configuracion.
#
#   bash install.sh              instala
#   bash install.sh --check      solo comprueba dependencias
#   bash install.sh --no-hooks   scripts y regla, sin tocar settings.json
#   bash install.sh --sin-scout    instala sin vault_scout ni su linea en la regla
#   bash install.sh --quitar-scout borra vault_scout y revierte la regla
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CLAUDE_DIR/scripts"
RULES="$CLAUDE_DIR/rules"
HOOKS="$CLAUDE_DIR/hooks"
SETTINGS="$CLAUDE_DIR/settings.json"
MEM_DB="${CLAUDE_MEM_DB:-$HOME/.claude-mem/claude-mem.db}"

SOLO_CHECK=0
SIN_HOOKS=0
SIN_SCOUT=0
QUITAR_SCOUT=0
for a in "$@"; do
    case "$a" in
        --check) SOLO_CHECK=1 ;;
        --no-hooks) SIN_HOOKS=1 ;;
        --sin-scout) SIN_SCOUT=1 ;;
        --quitar-scout) QUITAR_SCOUT=1 ;;
    esac
done

# Descableado del scout: borra el script y revierte el bloque de la regla.
# Va antes de comprobar dependencias porque quitar algo no debe exigir tenerlo todo.
if [ "$QUITAR_SCOUT" -eq 1 ]; then
    command rm -f "$DEST/vault_scout.py"
    # El interruptor sobrevive si no se borra, y una reinstalacion posterior
    # arrancaria encendida pese al apagado por defecto. La cache de bytecode
    # es inerte pero igual de residual.
    command rm -f "$CLAUDE_DIR/.vault-scout-on"
    command rm -f "$DEST"/__pycache__/vault_lookup.cpython-*.pyc
    regla="$RULES/vault-lookup.md"
    if [ -f "$regla" ]; then
        # Marcas desparejadas: el rango de sed no encontraria el cierre y
        # borraria hasta el final del fichero, llevandose por delante lo que
        # el usuario hubiera anadido. Mejor no tocar la regla y decirlo.
        abre=$(grep -c '<!-- vault-scout -->' "$regla" || true)
        cierra=$(grep -c '<!-- /vault-scout -->' "$regla" || true)
        if [ "$abre" != "$cierra" ]; then
            echo "! $regla tiene $abre marcas de apertura y $cierra de cierre" >&2
            echo "! no se toca la regla: quita el bloque a mano entre las marcas" >&2
        elif [ "$abre" != "0" ]; then
            sed -i.bak '/<!-- vault-scout -->/,/<!-- \/vault-scout -->/d' "$regla"
            command rm -f "$regla.bak"
        fi
    fi
    echo "✓ scout descableado — el resto del trick sigue funcionando"
    exit 0
fi

# ---------------------------------------------------------------- dependencias
fallos=0
avisos=0
ok()    { printf '  ✓ %s\n' "$1"; }
falta() { printf '  ✗ %s\n' "$1"; fallos=$((fallos+1)); }
aviso() { printf '  ! %s\n' "$1"; avisos=$((avisos+1)); }

echo "Dependencias obligatorias:"

if command -v python3 >/dev/null 2>&1; then
    ok "python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
else
    falta "python3 no encontrado"
fi

# El modulo sqlite3 de Python debe traer FTS5: es lo que hace la busqueda por
# contenido 56 veces mas rapida que grep. Sin el, todo lo demas sigue yendo.
if python3 - <<'PY' 2>/dev/null
import sqlite3, sys
c = sqlite3.connect(":memory:")
try:
    c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except Exception:
    sys.exit(1)
PY
then ok "sqlite3 de Python con FTS5"
else aviso "el sqlite3 de Python no trae FTS5: la busqueda por contenido no funcionara"
fi

if command -v git >/dev/null 2>&1; then
    ok "git $(git --version | awk '{print $3}')"
else
    falta "git no encontrado — hace falta para spokes, documentos y commits"
fi

if command -v graft >/dev/null 2>&1; then
    ok "graft $(graft --version 2>/dev/null | head -1)"
else
    falta "graft no encontrado — npm i -g @parcadei/graft (o el paquete que uses)"
fi

if [ -f "$MEM_DB" ]; then
    n=$(sqlite3 -readonly "$MEM_DB" "SELECT count(*) FROM observations;" 2>/dev/null || echo "?")
    ok "claude-mem en $MEM_DB ($n observaciones)"
else
    falta "no hay base de claude-mem en $MEM_DB (CLAUDE_MEM_DB para cambiarla)"
fi

if [ -d "$CLAUDE_DIR" ]; then
    ok "configuracion de Claude Code en $CLAUDE_DIR"
else
    falta "no existe $CLAUDE_DIR (CLAUDE_CONFIG_DIR para cambiarlo)"
fi

echo "Opcionales:"

if command -v sqlite3 >/dev/null 2>&1; then
    ok "sqlite3 (CLI)"
else
    aviso "sin sqlite3 CLI: este instalador no podra listar proyectos"
fi

if command -v rg >/dev/null 2>&1; then
    ok "ripgrep — busqueda en documentos"
else
    aviso "sin ripgrep: se usara grep para los documentos (mas lento, funciona igual)"
fi

OBS_JSON=""
for c in "$HOME/Library/Application Support/obsidian/obsidian.json" \
         "$HOME/.config/obsidian/obsidian.json" \
         "${APPDATA:-/nonexistent}/obsidian/obsidian.json"; do
    [ -f "$c" ] && { OBS_JSON="$c"; break; }
done
if [ -n "$OBS_JSON" ]; then
    ok "Obsidian detectado ($OBS_JSON)"
else
    aviso "Obsidian no detectado: los vaults se generan igual, pero habra que darlos de alta a mano"
fi

HUBS="${GRAFT_MEM_VAULT_HUBS:-$HOME/ai/HUB}"
if [ -d "$HUBS" ]; then
    ok "hubs de proyecto en $HUBS ($(find "$HUBS" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ') proyectos)"
else
    aviso "no existe $HUBS: el autodescubrimiento no encontrara nada. Usa GRAFT_MEM_VAULT_HUBS o escribe la config a mano"
fi

echo
if [ "$fallos" -gt 0 ]; then
    echo "✗ faltan $fallos dependencias obligatorias — no instalo nada"
    exit 1
fi
[ "$avisos" -gt 0 ] && echo "instalable con $avisos avisos"
if [ "$SOLO_CHECK" -eq 1 ]; then
    echo "(--check: no se ha instalado nada)"
    exit 0
fi

# ---------------------------------------------------------------- instalacion
echo
mkdir -p "$DEST" "$RULES" "$HOOKS"
cp "$DIR/graft_mem_vault.py" "$DIR/refresh_vaults.py" "$DIR/vault_lookup.py" "$DEST/"
chmod +x "$DEST/graft_mem_vault.py" "$DEST/refresh_vaults.py" "$DEST/vault_lookup.py"
echo "✓ scripts en $DEST/"

cp "$DIR/rules/vault-lookup.md" "$RULES/"

# El bloque del scout lo escribe el instalador, no el fichero de la regla: el cp
# de arriba sobrescribe la regla en cada instalacion y se llevaria por delante
# cualquier linea anadida a mano.
if [ "$SIN_SCOUT" -eq 1 ]; then
    command rm -f "$DEST/vault_scout.py"
    echo "· scout omitido (--sin-scout)"
else
    cp "$DIR/vault_scout.py" "$DEST/"
    chmod +x "$DEST/vault_scout.py"
    cat >> "$RULES/vault-lookup.md" <<'BLOQUE_SCOUT'

<!-- vault-scout -->
## Consulta exploratoria: delegar el filtrado

Cuando la pregunta es un **concepto** («¿ya intentamos X?», «qué se hizo para Y»)
y no un id que ya conoces, usa el scout: recolecta, recorta y devuelve hechos
anclados a su fuente en vez de volcar observaciones enteras.

```bash
python3 ~/.claude/scripts/vault_scout.py "<pregunta>"     # veredicto con ids
python3 ~/.claude/scripts/vault_scout.py --dry-run "<p>"  # sin llamar a ningún modelo
python3 ~/.claude/scripts/vault_scout.py --check          # interruptor y motor
```

Con id, uuid o ruta ya conocidos, `vault_lookup.py` directo: el scout solo añadiría
latencia.

**Apagado por defecto.** Encender: `touch ~/.claude/.vault-scout-on`. Apagado, la
salida del scout es la de `vault_lookup.py` sin ninguna diferencia — excepto
`--check`, que informa igual con el interruptor apagado.
<!-- /vault-scout -->
BLOQUE_SCOUT
    echo "✓ scout en $DEST/vault_scout.py (apagado; touch ~/.claude/.vault-scout-on)"
fi
echo "✓ regla en $RULES/vault-lookup.md"

if [ "$SIN_HOOKS" -eq 1 ]; then
    echo "· hooks omitidos (--no-hooks)"
else
    cp "$DIR/hooks/vault-session-start.py" "$DIR/hooks/vault-read-nudge.py" \
       "$DIR/hooks/vault-prompt-search.py" "$HOOKS/"
    chmod +x "$HOOKS/vault-session-start.py" "$HOOKS/vault-read-nudge.py" \
             "$HOOKS/vault-prompt-search.py"
    echo "✓ hooks en $HOOKS/"

    # Registro idempotente en settings.json, con copia previa.
    python3 - "$SETTINGS" "$HOOKS" <<'PY'
import json, os, shutil, sys, time
settings, hooks_dir = sys.argv[1], sys.argv[2]
if not os.path.exists(settings):
    print(f"  ! no existe {settings}; registra los hooks a mano (ver README)")
    raise SystemExit(0)
shutil.copy2(settings, f"{settings}.bak-{int(time.time())}")
d = json.load(open(settings))
h = d.setdefault("hooks", {})
nuevos = [
    ("SessionStart", "startup|resume|compact|clear", "vault-session-start.py"),
    ("PreToolUse", "Read", "vault-read-nudge.py"),
    ("UserPromptSubmit", None, "vault-prompt-search.py"),
]
n = 0
for evento, matcher, script in nuevos:
    if any(script in x.get("command", "")
           for g in h.get(evento, []) for x in g.get("hooks", [])):
        continue
    grupo = {"hooks": [{"type": "command",
                        "command": f"python3 {hooks_dir}/{script}",
                        "timeout": 10}]}
    if matcher:
        grupo["matcher"] = matcher
    h.setdefault(evento, []).append(grupo)
    n += 1
json.dump(d, open(settings, "w"), indent=2)
print(f"✓ {n} hooks registrados en settings.json (copia previa en .bak-*)")
PY
fi

echo
echo "Siguiente paso — generar los vaults:"
echo "  python3 $DEST/refresh_vaults.py"
echo "(la primera vez escribe la configuracion en ~/.config/graft-mem-vault/projects.json)"
echo
echo "Despues, desde cualquier repo con vault:"
echo "  python3 $DEST/vault_lookup.py <fichero-concepto-o-id>"
echo
if command -v sqlite3 >/dev/null 2>&1 && [ -f "$MEM_DB" ]; then
    echo "Proyectos con memoria en claude-mem:"
    sqlite3 -readonly "$MEM_DB" \
        "SELECT '  ' || project || '  (' || count(*) || ' obs)' FROM observations GROUP BY project ORDER BY count(*) DESC LIMIT 10;" \
        2>/dev/null || true
fi
