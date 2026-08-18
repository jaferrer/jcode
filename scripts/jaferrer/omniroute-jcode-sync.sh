#!/usr/bin/env bash
# Refreshes [[providers.omniroute.models]] in jcode's config.toml with
# OmniRoute's current combo list (owned_by == "combo"). Silent, fast,
# never blocks jcode startup on failure.
#
# Nota: desde agosto 2026 el flag `jcode --omniroute` ya hace este sync
# internamente al arrancar (src/cli/dispatch.rs::sync_omniroute_combos).
# Este wrapper sigue siendo útil para lanzamientos de jcode SIN --omniroute
# (mantiene config.toml fresca para --provider-profile omniroute y /model).
set -uo pipefail

CONFIG="$HOME/.jcode/config.toml"
BASE_URL="${OMNIROUTE_BASE_URL:-http://localhost:20128}"

[ -f "$CONFIG" ] || exit 0
grep -q '^\[providers.omniroute\]' "$CONFIG" || exit 0

MODELS_JSON=$(curl -fsS --max-time 2 "$BASE_URL/v1/models" 2>/dev/null) || exit 0
[ -n "$MODELS_JSON" ] || exit 0

export OMNIROUTE_MODELS_JSON="$MODELS_JSON"
python3 "$HOME/.local/bin/omniroute-jcode-sync.py" "$CONFIG"
