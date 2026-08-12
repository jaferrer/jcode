# Paridad de graft-mem-vault entre Claude Code, Codex y jcode

**Fecha:** 2026-08-11
**Estado:** diseño aprobado, pendiente de plan de implementación
**Alcance:** fase 1 (lectura en los tres harnesses + escritura desde jcode). La fase 2 queda explícitamente fuera.

## Problema

El pipeline graft-mem-vault funciona hoy en Claude Code y Codex, y no en jcode. Se quiere que los tres harnesses trabajen por igual: inyección de contexto al inicio, persistencia de lo aprendido al cerrar o compactar la sesión, y búsqueda con el scout sobre graft + mem + vault.

El port Claude Code → Codex fue casi gratuito porque Codex adoptó el mismo contrato de hooks: los scripts emiten por stdout

```json
{"systemMessage": "…",
 "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "…"}}
```

y el harness funde `additionalContext` en el contexto del modelo. `install-codex.sh:200-205` registra los mismos scripts sin cambiarles una línea.

jcode no implementa ese contrato, y ahí se rompe la cadena.

## Restricción técnica verificada

Todo lo siguiente está comprobado contra el binario de jcode v0.75.3 y sus logs, no contra documentación.

**Los hooks de jcode no pueden inyectar contexto.** Existen seis eventos — `session_start`, `session_end`, `turn_start`, `turn_end`, `pre_tool`, `post_tool` — y la documentación embebida en el binario los describe así:

> `turn_end`, `session_start`, `session_end`, and `post_tool` are **observers**: spawned detached, fire-and-forget. They can never block or slow the agent; failures are only logged.

`pre_tool` es la única puerta, y es binaria:

> Exit 0: allow the call. Exit 2: block the call. The hook's stderr (trimmed, capped at 2000 chars) is returned to the model **as the tool error**.

No hay evento en el momento de enviar el prompt, ni campo análogo a `additionalContext`. El único texto que llega al modelo desde un hook viene enmarcado como error de una llamada a tool.

**Pero jcode sí lee ficheros en el ensamblado del prompt.** Orden de montaje, extraído del binario:

1. prompt base
2. módulos de capacidad
3. guía de self-dev
4. `AGENTS.md` — proyecto `./AGENTS.md` y global `~/AGENTS.md`
5. **prompt overlay** — `./.jcode/prompt-overlay.md` y `~/.jcode/prompt-overlay.md`

Esa es la vía. Un hook no puede inyectar, pero sí puede **escribir** uno de esos ficheros, y jcode lo monta en el prompt por su cuenta.

**El resto del instrumental ya es compatible**, sin trabajo alguno:

- MCP: jcode lee `~/.claude.json` (global y por proyecto), `.mcp.json`, `~/.claude/mcp.json`, `~/.jcode/mcp.json`, `.jcode/mcp.json` y `~/.codex/config.toml` (`mcp_servers`). Solo transporte stdio; las entradas HTTP/SSE se reconocen y se ignoran. Verificado en sus logs: ya conectó `apple-mcp`, `claude-peers`, `codebase-memory-mcp`, `context7`, `fastedit` y `notebooklm-mcp` sin configuración adicional.
- Skills: lee `.claude/skills`, `.codex/skills` y `.jcode/skills`. Las 33 skills presentes en `~/.jcode/skills/` son byte-idénticas a las de origen. Añade una clave aditiva `compatibility` al frontmatter, que no es un requisito.
- No lee `CLAUDE.md`. Las reglas deben vivir en `AGENTS.md` o en el overlay.

## Quién escribe hoy la memoria

Conviene fijarlo porque determina el alcance: **graft-mem-vault no escribe**. Abre el store en solo lectura —

```python
sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
```

— y genera el vault markdown a partir de `~/.claude-mem/claude-mem.db`, git y graft. Quien escribe ese `.db` es **claude-mem**, un plugin independiente (marketplace `thedotmack`, v13.12.4).

`~/.claude/hooks/pre-compact.mjs` no interviene en esto: es el auto-handoff, que parsea el transcript y escribe markdown en `thoughts/shared/handoffs/`.

**claude-mem expone un CLI con parámetro de plataforma:**

```
Usage: claude-mem hook <platform> <event>
Platforms: claude-code, codex, cursor, antigravity-cli, raw
Events: context, session-init, observation, summarize, user-message
```

La plataforma `raw` es el adaptador genérico. Comprobado que `hook raw context` devuelve el contrato completo:

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "…"},
 "systemMessage": "…"}
```

Es decir: claude-mem ya produce la carga útil correcta. Lo único que falta en jcode es alguien que la recoja y la deposite donde jcode mira.

**Consecuencia estructural:** la vía de escritura no necesita inyección. Extraer y guardar solo requiere dispararse en el momento correcto y poder leer el transcript — y para eso los observadores de jcode bastan. Solo la vía de lectura necesitaba inyección, y se resuelve por fichero.

## Arquitectura

Tres capas. Ningún componente del pipeline existente se modifica; los únicos ficheros tocados son de configuración (`~/.jcode/config.toml`, `~/.zshrc`, `.gitignore`, `.mcp.json`).

| Capa | Piezas | Cambio |
|---|---|---|
| Núcleo | `claude-mem`, `graft_mem_vault.py`, `vault_lookup.py`, `vault_scout.py`, `refresh_vaults.py` | ninguno |
| Adaptador por harness | hooks de Claude Code (ya existen), hooks de Codex (ya existen), **puente de jcode (nuevo)** | solo se añade jcode |
| Acceso transversal | **servidor MCP del vault (nuevo)** | nuevo, sirve a los tres |

Claude Code y Codex reciben por inyección de hook; jcode recibe por fichero que su ensamblador ya lee. Distinto transporte, misma información.

## Componentes

### `jcode-vault-bridge.py` (nuevo)

Vive en `~/.local/bin/`. Dos modos.

**`--context`** (lectura). Invoca `claude-mem hook raw context` y `vault-session-start.py`, parsea el campo `additionalContext` de cada salida y los concatena en orden fijo —primero el del vault, después el de claude-mem—, cada uno bajo su propio encabezado markdown, y escribe el resultado en `.jcode/prompt-overlay.md`. Sin deduplicación: son fuentes distintas y el solape, si lo hay, es informativo.

**`--summarize` / `--observation`** (escritura). Passthrough a `claude-mem hook raw summarize` y `claude-mem hook raw observation`, reenviando el payload que recibe por stdin.

### Cableado en `~/.jcode/config.toml`

```toml
[hooks]
session_start = "~/.local/bin/jcode-vault-bridge.py --context"
session_end   = "~/.local/bin/jcode-vault-bridge.py --summarize"
post_tool     = "~/.local/bin/jcode-vault-bridge.py --observation"
```

### Llamada desde el wrapper `jcode()`

La función `jcode()` de `~/.zshrc` — la que ya sincroniza los combos de OmniRoute — llama además a `jcode-vault-bridge.py --context` antes de lanzar el binario.

Es redundante con el hook `session_start` **a propósito**. El hook es fire-and-forget y no está determinado que gane la carrera contra el ensamblado del prompt; el wrapper escribe antes de que jcode arranque, sin carrera posible. El hook cubre los casos sin wrapper: `resume` y `/compact`. Las dos rutas son idempotentes.

### Servidor MCP del vault (nuevo)

Envoltorio fino sobre `vault_lookup.py` y `vault_scout.py`, registrado en `.mcp.json`, que los tres harnesses leen. Efecto colateral valioso: **le devuelve el scout a Codex**, que lo perdió en su port junto con el stub `rules/vault-lookup.md`.

## Flujo de datos

```
arranque:  wrapper jcode() → bridge --context
                           → claude-mem hook raw context + vault-session-start.py
                           → .jcode/prompt-overlay.md
                           → jcode lo monta en el paso 5 del prompt

durante:   post_tool  → bridge --observation → claude-mem → claude-mem.db

al cerrar: session_end → bridge --summarize → claude-mem → claude-mem.db
                       → refresh_vaults.py regenera el vault markdown

consulta:  modelo → tool MCP vault_scout → graft + claude-mem.db + vault
```

## Manejo de errores

Fail-open en todo, siguiendo el patrón de los hooks existentes: sin resultados, emitir `{}` y salir con 0.

El puente añade dos reglas propias:

- Timeout de 5 s en las llamadas a claude-mem.
- **Si algo falla, no tocar el overlay existente.** Dejarlo con contenido viejo es preferible a vaciarlo: contexto desactualizado es mejor que ninguno, y un overlay vacío es indistinguible de «este proyecto no tiene memoria».

`.jcode/` se añade a `.gitignore`: el overlay contiene material del vault y no debe acabar versionado.

## Pruebas

`self_check_jcode.py`, calcado de `self_check_codex.py`:

1. `--context` produce un overlay no vacío cuando hay memoria.
2. Dos ejecuciones seguidas son idempotentes.
3. Con claude-mem caído o ausente, el overlay previo sobrevive intacto.
4. Todos los modos salen con 0 aunque fallen.

## Decisiones tomadas

- **Overlay por proyecto** (`.jcode/prompt-overlay.md`), no global. El vault es por proyecto; un overlay global mezclaría contextos de repos distintos.
- **Reutilizar claude-mem** como único escritor, en vez de dar a graft-mem-vault un store propio. Mantiene un solo `claude-mem.db`, un solo generador de vault, y preserva el desacoplamiento en solo lectura que hace limpio todo el montaje.
- **Redundancia deliberada** entre wrapper y hook `session_start`, por la carrera no resuelta.

## Riesgos e incógnitas

- **No está verificado si `session_start` se ejecuta antes del ensamblado del prompt.** De ahí la redundancia con el wrapper. Si resultara que sí gana la carrera de forma fiable, el wrapper podría retirarse después.
- **No está verificado si `/compact` en jcode dispara `session_start`.** Si no lo hiciera, tras compactar el overlay quedaría con el contenido de la sesión anterior — degradación aceptable, no pérdida.
- `claude-mem hook raw` con eventos de escritura no se ha ejecutado todavía; solo se ha probado `context`, que es de lectura. La implementación debe validar `summarize` y `observation` contra una base de pruebas antes de apuntarlos al `.db` real de 159 MB.
- La concurrencia de regeneración está cubierta por el lock fijo `/tmp/graft-mem-vault-refresh.lock` (TTL 300 s), que al no estar namespaceado por harness serializa correctamente entre los tres.

## El aparato que no cruza: `vault-read-nudge.py`

Conviene decirlo explícitamente en vez de que se descubra al usarlo. En Claude Code este hook se engancha a `PreToolUse` con matcher `Read` (en Codex, `apply_patch|Edit|Write`) y avisa por `additionalContext` de que el fichero que se va a tocar tiene historia en el vault.

En jcode **no tiene equivalente en fase 1**. `pre_tool` existe, pero su único canal hacia el modelo es el stderr con `exit 2`, que además de llegar enmarcado como error **bloquea la llamada**. Usarlo para avisos significaría abortar cada `Read` del proyecto: técnicamente posible, inaceptable en la práctica. Se descarta de forma consciente.

Mitigación parcial, sin construir nada: el overlay lleva el contexto del proyecto al arranque, y el scout por MCP permite al modelo consultar la historia de un fichero concreto cuando le interese. Se pierde el aviso automático, no el acceso al dato.

La fase 2 lo resolvería de otra manera —volcando esas observaciones al grafo de memoria de jcode, que las inyecta por similitud— sin necesidad de un hook por fichero.

## Fuera de alcance

**Fase 2 — recall pasivo por turno en jcode.** jcode mantiene un grafo de memoria propio en `~/.jcode/memory/projects/<hash>.json`, JSON plano con embeddings de 384 dimensiones (all-MiniLM vía ONNX), escribible desde fuera. Volcar ahí las observaciones del vault haría que jcode las inyectase solo por similitud en cada turno, replicando lo que `vault-prompt-search.py` hace en Claude Code y Codex.

Se decide con la fase 1 ya en funcionamiento. No forma parte de este spec.
