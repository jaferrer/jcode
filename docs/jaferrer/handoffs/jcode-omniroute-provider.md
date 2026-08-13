# Handoff: OmniRoute como provider de jcode con combos en el picker

**Fecha**: 2026-08-11
**Estado**: resuelto y verificado por el usuario ("ya funciona!!!")

## Contexto

jcode (harness Rust) soporta providers `openai-compatible` nombrados vía
`[providers.<name>]` en `~/.jcode/config.toml`. Se quería que **OmniRoute**
(gateway local en `http://localhost:20128/v1`) apareciera en el picker
`/model` mostrando solo sus "combos" curados (meta-router), no los 984
modelos crudos que reexpone de los proveedores subyacentes.

## Hallazgo clave

- `model_catalog = true` + la caché (`~/.jcode/cache/omniroute_models.json`)
  **no** rellenan el picker interactivo `/model`. El picker solo lista lo
  que esté explícito como `[[providers.<name>.models]]` en `config.toml`.
- El flag `--provider-routing` de `jcode provider add` es la herramienta
  **equivocada** para esto: activa el modo estilo-OpenRouter de jcode, que
  relabela el provider como `openrouter` en el picker y explota cada
  modelo en variantes de reasoning-effort (low/med/high/minimal/none/xhigh).
  Causó una explosión de "infinitos modelos" — revertido.
- De los 984 modelos que expone `/v1/models` de OmniRoute, solo **47**
  tienen `"owned_by": "combo"` — esos son los combos reales del
  meta-router (`auto/best-coding`, `auto/pro-reasoning`, `Coding-paid`,
  `Arquitecto-pago`, `Standalone-Codex-5.6-Luna`, etc.).

## Fix aplicado

En `~/.jcode/config.toml`, bloque `[providers.omniroute]` (fuera de este
repo, config local de jcode):

```toml
[providers.omniroute]
type = "openai-compatible"
base_url = "http://localhost:20128/v1"
auth = "none"
default_model = "auto/best-coding"
requires_api_key = false
model_catalog = true

[[providers.omniroute.models]]
id = "auto/best-coding"
# ... 46 entradas más, una por combo (owned_by == "combo")
```

Sin `provider_routing` ni `allow_provider_pinning` — esos campos son los
que causan el mislabel a "openrouter" y deben quedar ausentes.

Backup del config previo: `~/.jcode/config.toml.bak-<timestamp>`.

## Cómo reproducir/extender para otro gateway similar

1. `curl <base_url>/v1/models` (o `ctx_execute`) y filtrar por el campo
   que el gateway use para marcar sus meta-rutas (aquí `owned_by=="combo"`).
2. Añadir cada id como `[[providers.<name>.models]]` a mano — `jcode
   provider add` solo acepta un `-m/--model` por invocación, no hace batch.
3. **Nunca** usar `--provider-routing` salvo que el gateway sea literalmente
   OpenRouter o quieras el comportamiento de variantes de reasoning-effort.
4. `auth-test` puede dar un falso 401 con `auth="none"` (bug conocido de
   jcode, visto también con el profile `ollama-local`) — no bloquea el uso
   real vía `jcode run` o la TUI.
5. Arranque: `jcode` requiere TTY interactivo (no funciona vía subagente/
   pipe) — lanzarlo desde una terminal del usuario.

## Documentación relacionada

- `resources/tools/omniroute.md` — doc general de OmniRoute (puerto de
  dashboard corregido a 20128 en esta misma sesión).
- `resources/tools/jcode.md` — doc general de jcode.

## Actualización 2026-08-11 (tarde): filtro de combos corregido + auto-sync

**Error en el filtro inicial**: `owned_by == "combo"` no basta. OmniRoute
marca con `owned_by=="combo"` tanto los combos nombrados por el usuario
como sus **38 enrutadores genéricos `auto/*`** — de fábrica, presentes en
cualquier instalación, no configurados por el usuario. Confirmado por
hallazgo previo en vault (obs 29323, 2026-08-01): *"8 named combos... 38
auto/* correctly excluded"* — el hub de OmniRoute ya filtraba por
`'/' not in id`, y yo no lo hice al construir el filtro en esta sesión.

**Fix**: filtro correcto es `owned_by == "combo" and "/" not in id`.

**Auto-sync en cada arranque**: para no tener que repetir el proceso
manual cada vez que el usuario da de alta/borra combos en OmniRoute:

- `~/.local/bin/omniroute-jcode-sync.sh` (bash) + `.py` (python) — hacen
  `GET /v1/models`, filtran con el criterio de arriba, y solo reescriben
  `[[providers.omniroute.models]]` en `~/.jcode/config.toml` si el set
  cambió. Timeout 2s, falla en silencio si OmniRoute está parado.
- Función `jcode()` añadida al final de `~/.zshrc` que llama al script de
  sync y luego ejecuta el binario real (`command jcode "$@"`).
- Efecto: cada vez que el usuario abre una terminal nueva y lanza
  `jcode`, el picker ya refleja los combos nombrados actuales de
  OmniRoute sin intervención manual.

Verificado con los 9 combos reales tras la corrección (6 `Qoder-*-valle`
dados de alta por el usuario + otros nombrados existentes, ninguno de los
38 `auto/*`).

## Actualización 2026-08-13: el sync ahora propaga `context_window`

**Síntoma**: los combos aparecían en el picker pero la sesión reportaba
200K de contexto aunque el modelo destino soportara 1M.

**Causa**: el sync escribía solo `id = "..."`. Sin `context_window`, jcode
no puede clasificar un id de combo nombrado por el usuario (no casa con
ninguna familia conocida) y cae en `DEFAULT_CONTEXT_LIMIT` = 200K. El
caché `~/.jcode/cache/omniroute_models.json` sí trae `context_length`,
pero es best-effort: envejece y no contiene los combos recién creados.

**Dónde vive el contexto en OmniRoute** (`~/.omniroute/storage.sqlite`):

- `model_context_overrides` (provider, model_id, real_context, source) —
  override manual o `auto:discovery` por modelo *subyacente*.
- `model_capabilities.limit_context` — sincronizado desde models.dev.
- Specs estáticos del código como último recurso.

Los combos **no** se configuran directamente: su ventana se calcula como
el **mínimo** del `contextLength` de sus modelos miembro, salvo que el
combo traiga un `context_length` propio. Ese valor ya se expone en
`/v1/models` por combo, así que el sync solo tiene que copiarlo.

**Fix**: `scripts/jaferrer/omniroute-jcode-sync.py` lee `context_length`
(o `max_input_tokens`) de cada combo y emite:

```toml
[[providers.omniroute.models]]
id = "Opus5-WA"
context_window = 1000000
```

Detalles del parche: la comparación de idempotencia ahora incluye la
ventana, de modo que un cambio de contexto sin cambio de ids también
dispara la reescritura; los ids se serializan con escapado TOML (varios
combos llevan espacios, y el escapado protege comillas); un combo sin
`context_length` se emite sin el campo, cayendo al comportamiento previo.

**Verificación**: test temporal en `jcode-base` que parsea el
`~/.jcode/config.toml` real y llama a `context_limit_for_model` (el mismo
resolutor que usan el widget de contexto y el presupuesto de compactación)
para cada combo. Los 18 resuelven su ventana real (1000000, 1048576 y
262144 para `Kimi2.7-code`), ninguno cae al default. Test eliminado tras
verificar; el mecanismo permanece cubierto por
`populate_context_limits_from_config_ref_seeds_global_cache`.

**Nota operativa**: el config se lee al arrancar. Tras un cambio de
combos hay que relanzar jcode (la función `jcode()` del `.zshrc` corre el
sync antes) o usar `/reload`.
