# Handoff — graft-mem-vault parity para jcode

Fecha: 2026-08-11
Rama: `feat/graft-mem-vault-jcode-parity`
Alcance: Tarea 9 del plan `docs/superpowers/plans/2026-08-11-graft-mem-vault-harness-parity.md`.

## Estado

La paridad base de `graft-mem-vault` para jcode quedó implementada y verificada en HEAD actual, que ya incluía el fix de T7 y T8. Este handoff documenta el cierre operativo, no introduce cambios funcionales.

La prueba interactiva real en jcode queda pendiente para el usuario porque `jcode` requiere un TTY interactivo. Se ejercitó el wrapper real con `zsh -ic 'cd /Users/ferrer/guia && jcode version --json'`: devolvió v0.75.3 y produjo `.jcode/prompt-overlay.md` no vacío. También se intentó abrir y cerrar `jcode repl` en un pseudo-TTY sin enviar mensajes al modelo, pero el REPL no terminó de forma cooperativa con `/exit`; el proceso de prueba se canceló y limpió. El usuario debe abrir una terminal nueva, entrar en `/Users/ferrer/guia`, lanzar `jcode` y comprobar:

1. que el modelo arranca con contexto recuperado del proyecto,
2. que la tool MCP `vault_scout` aparece disponible,
3. que al cerrar la sesión no aparece ningún error de hooks.

## Qué quedó instalado y dónde

Instalación jcode documentada por `tricks_ferrer/graft-mem-vault/install-jcode.sh`:

- Scripts copiados a `~/.jcode/graft-mem-vault/scripts/`:
  - `graft_mem_vault.py`
  - `refresh_vaults.py`
  - `vault_lookup.py`
  - `vault_scout.py`
- Hooks copiados a `~/.jcode/graft-mem-vault/hooks/`:
  - `vault-session-start.py`
  - `jcode-vault-bridge.py`
- Hooks cableados en `~/.jcode/config.toml`:
  - `session_start = "~/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py --context"`
  - `session_end = "~/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py --summarize"`
  - `post_tool = "~/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py --observation"`
- Wrapper manual esperado en `~/.zshrc`: invocar el puente antes de arrancar `jcode` para refrescar `.jcode/prompt-overlay.md`.
- MCP registrado en `.mcp.json` como servidor `vault`, ejecutando `python3 tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py`.

El puente no intenta inyectar contexto directamente desde hooks de jcode. Escribe `.jcode/prompt-overlay.md`, que jcode sí monta en el prompt. El orden del overlay es fijo: primero vault, luego memoria de sesiones.

## Contrato `raw` descubierto

El contrato usable con `claude-mem` es:

```bash
bun <worker-service.cjs> hook raw <event>
```

Con payload JSON por stdin. Eventos usados por el puente:

- `context`: lectura de contexto para startup.
- `summarize`: escritura/resumen al cerrar sesión.
- `observation`: escritura tras herramientas.

La salida relevante se lee de:

```json
{
  "hookSpecificOutput": {
    "additionalContext": "..."
  }
}
```

`jcode-vault-bridge.py` usa ese campo mediante `additional_context(raw)`. Si el worker no existe, falla, emite JSON inválido o tarda más del timeout, el hook hace fail-open y termina con exit code 0.

Payloads que sintetiza el puente cuando jcode no los proporciona completos:

- `cwd`: `JCODE_HOOK_CWD` o `os.getcwd()`.
- `session_id`: `JCODE_HOOK_SESSION_ID` o `jcode`.
- `tool_name`: `JCODE_HOOK_TOOL_NAME`, solo si existe.
- `source`: `startup` para `--context`.

## Incógnitas resueltas

- jcode no puede recibir `additionalContext` directamente desde hooks ordinarios. La vía estable es `.jcode/prompt-overlay.md`.
- El bridge debe exportar un `main` callable. Esta regresión está cubierta en `self_check_jcode.py` y pasa en HEAD.
- El servidor MCP sin dependencias responde por stdio JSON-RPC, protocolo `2024-11-05`, lista `vault_lookup` y `vault_scout`, y ejecuta un `tools/call` real.
- El almacén real de `claude-mem` no contiene filas `parity-probe` tras la verificación read-only.
- Los metadatos reales de `observations` y `session_summaries` siguen legibles en modo read-only.

## Incógnitas abiertas

- Carrera de `session_start`: sigue abierta la posibilidad de que el overlay se refresque demasiado tarde para el primer ensamblado del prompt si se depende solo del hook. Por eso se mantiene el wrapper manual en `~/.zshrc`.
- `/compact`: no quedó verificado si jcode vuelve a montar `.jcode/prompt-overlay.md` después de compactar o si conserva solo el contexto ya cargado.
- Prueba real interactiva de jcode: pendiente del usuario por requerir TTY.

## Fase 2 pendiente

La fase 2 queda pendiente. Debe decidir si se mantiene el overlay como contrato estable o si se implementa una integración más nativa con el ciclo de prompt de jcode. También debe cerrar la carrera de `session_start` y verificar explícitamente el comportamiento de `/compact`.

## Pruebas ejecutadas

Desde `tricks_ferrer/graft-mem-vault`:

```bash
python3 self_check_jcode.py
python3 self_check_codex.py
python3 self_check_hooks.py
bash -n install-jcode.sh
python3 -m py_compile hooks/jcode-vault-bridge.py mcp/vault_mcp_server.py
zsh -n ~/.zshrc
python3 -m json.tool .mcp.json
```

Resultados observados:

- `self_check_jcode.py`: `OK — extracción y composición del overlay`.
- `self_check_codex.py`: `OK — extractores Codex e instalación idempotente`.
- `self_check_hooks.py`: `OK — 3 hooks: contrato de salida y fail-open ante entradas rotas`.
- `bash -n install-jcode.sh`: sin errores.
- `py_compile` de bridge/MCP: sin errores.
- `zsh -n ~/.zshrc`: sin errores.
- `.mcp.json`: JSON válido.
- Los tres comandos exactos de `~/.jcode/config.toml` se ejecutaron contra el bridge instalado (`session_start`, `post_tool`, `session_end`): todos devolvieron exit 0 y stderr vacío.
- El bridge instalado y el versionado coinciden por SHA-256; la ejecución real dejó `.jcode/prompt-overlay.md` de 2136 bytes.

MCP real por stdio desde la entrada exacta de `.mcp.json`:

- `initialize` respondió `protocolVersion: 2024-11-05` y `serverInfo.name: vault`.
- `tools/list` devolvió `vault_lookup` y `vault_scout`.
- `tools/call` real a `vault_scout` con `pregunta: graft mem vault jcode parity` respondió correctamente con texto MCP, aunque sin hits en el vault para esa consulta.

End-to-end jcode real:

- La ejecución real mediante el wrapper `zsh -ic 'cd /Users/ferrer/guia && jcode ... run --json'` devolvió exit 0 y respondió exactamente `OVERLAY_ACCEPTED` al pedirle verificar la cabecera `Memoria de sesiones` del contexto de proyecto.
- La ejecución no usó herramientas y no dejó filas marcadas `public-run-acceptance` en la base read-only. El JSON reportó `qwen3.5:4b-nvfp4`; aunque se solicitó `--provider ollama`, el campo `provider` reportado por jcode fue `openrouter`, una discrepancia de routing fuera del alcance de este cambio.

Almacén real de claude-mem, consultado read-only (`file:...mode=ro`):

- `observations`: 0 filas `parity-probe`.
- `session_summaries`: 0 filas `parity-probe`.
- Metadatos legibles:
  - `observations`: 34 573 filas.
  - `session_summaries`: 5 616 filas.

## Limpieza y residuos

Scratch eliminado porque era inequívocamente de estas pruebas:

- `/tmp/parity-mem-data`
- `/tmp/parity-probe`

Backups deliberados conservados, no borrar automáticamente:

- `~/.claude-mem/claude-mem.db.bak-parity`: 159M.
- `~/.jcode/config.toml.bak-parity`: 5.5K.
- `~/.zshrc.bak-parity`: 8.8K.

Proceso `pid 14657` conservado. Sigue siendo:

```text
/opt/homebrew/Cellar/bun/1.3.11/bin/bun /Users/ferrer/.claude/plugins/cache/thedotmack/claude-mem/13.15.0/scripts/worker-service.cjs --daemon
```

No se mató porque puede ser un daemon activo reutilizado de `claude-mem`, no un scratch inequívoco.

## Advertencia de repo

Antes de esta tarea ya había cambios locales ajenos en el repo. No se tocaron ni restauraron. El commit final debe incluir solo este fichero de handoff.
