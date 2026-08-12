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
