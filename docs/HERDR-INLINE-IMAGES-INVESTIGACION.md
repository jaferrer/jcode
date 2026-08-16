# Herdr + xterm.js + jcode: imágenes inline no visibles — investigación y fix propuesto

Fecha: 2026-08-16
Repo: `/Users/ferrer/ai/HUB/jcode`
Estado: diagnóstico completo, fix propuesto sin implementar.
Documentos relacionados: `docs/HERDR.md`, `docs/HERDR-IMAGES-HANDOFF.md`.

## 1. Relación Herdr ↔ xterm.js ↔ jcode

- **Herdr 0.8.0** es un multiplexor de agentes cuyos panes renderizan con **xterm.js**.
- Herdr **enmascara el terminal exterior**: en cada pane exporta `TERM=xterm-256color`
  y `HERDR_ENV=1` (+ `HERDR_SOCKET_PATH`, `HERDR_PANE_ID`, `HERDR_TAB_ID`,
  `HERDR_WORKSPACE_ID`, `HERDR_SESSION`), ocultando el terminal real (Ghostty).
- **xterm.js no soporta gráficos Kitty/Sixel** por defecto. Además, según
  `docs/HERDR-IMAGES-HANDOFF.md`, Herdr **consume las secuencias APC Kitty emitidas
  por programas dentro del pane y descarta el placement** → emitir Kitty al pane
  no muestra nada.
- Herdr ofrece una API propia: JSON-RPC por el socket Unix `HERDR_SOCKET_PATH`,
  método `pane.graphics.set` (sin posición explícita; usa el cursor actual del pane).

jcode tiene **dos caminos de imagen independientes**:

| Camino | Código | Dónde corre | Estado en Herdr |
|---|---|---|---|
| Tool `read` / headless | `crates/jcode-terminal-image/src/display.rs` (`display_image`, `ImageProtocol::Herdr`) | **daemon** `jcode serve` | Integración `pane.graphics.set` existe pero está **muerta en la práctica** (ver causa 2) |
| Renderer inline del transcript (chat TUI) | `crates/jcode-tui/src/tui/ui_inline_image.rs` + picker en `crates/jcode-tui-mermaid/src/mermaid_runtime.rs` | **cliente TUI** (el proceso en el pane) | **Sin integración Herdr**: cae a Halfblocks |

## 2. Evidencia recopilada (2026-08-16)

- Clientes TUI del usuario corren en panes Herdr con `TERM=xterm-256color`,
  `HERDR_ENV=1`, `HERDR_SOCKET_PATH=/Users/ferrer/.config/herdr/sessions/repo-term/herdr.sock`.
- Log `~/.jcode/logs/jcode-2026-08-16.log`:
  - `Mermaid picker init: mode=Fast multiplexer=herdr env_protocol=None` → el picker
    del TUI cae a **Halfblocks** dentro de Herdr (en Ghostty directo detecta `Some(Kitty)`).
  - `SidePaneImages: appended 1 live inline image(s) (total=8)` → **las imágenes
    generadas SÍ llegan al cliente**; el pipeline server→cliente funciona.
- Binarios: clientes (PIDs 90110, 30579) y daemon (`serve`, PID 3899) ya ejecutan
  `527895b57-dirty` (inode 59762293 coincide). El pendiente del handoff anterior
  (procesos con binario viejo) **está resuelto**.
- Config usuario: `pin_images = true` en `~/.jcode/config.toml`.
- Daemon `serve` arrancado con `TERM=xterm-ghostty` y **sin `HERDR_*`** en su env.
- Sin warnings de materialización/decodificación de imágenes en el log.

## 3. Causas (ordenadas por probabilidad)

### Causa 1 (principal): el renderer inline del TUI no tiene integración Herdr

- El picker (`mermaid_runtime.rs::fast_picker`) infiere protocolo por env
  (`infer_protocol_from_env`). Dentro de Herdr: `TERM=xterm-256color` sin pistas
  → `env_protocol=None` → **Halfblocks**.
- `decide_picker_init_mode` por defecto usa `PickerInitMode::Fast` (sin probe stdio;
  el probe puede bloquear 2 s el arranque). Override: `JCODE_MERMAID_PICKER_PROBE=1`.
- Con Halfblocks las imágenes raster se pintan como bloques Unicode + nota de
  fallback (`TERMINAL_IMAGE_FALLBACK_NOTE`); los diagramas mermaid caen a texto
  fuente (`native_image_protocol_available()` excluye Halfblocks).
- **La API `pane.graphics.set` NO está cableada a este camino**: las imágenes
  generadas en el chat nunca usan el socket de Herdr.

### Causa 2: el camino `pane.graphics.set` existente está muerto en la práctica

`display_image` (tool `read`) corre en el **daemon**, no en el cliente del pane:

- El daemon se arrancó con `TERM=xterm-ghostty` y sin `HERDR_*` →
  `ImageProtocol::detect()` nunca devuelve `Herdr` (detectaría Kitty por TERM y
  emitiría APC al vacío).
- Su stdout no es TTY → `can_display_to_stdout()` devuelve false siempre
  (`display.rs:302`). Guarda anti-deadlock para NDJSON headless.

Resultado: la integración del handoff anterior solo funciona en probes standalone
con stdout TTY y env Herdr, nunca en una sesión TUI real.

### Causa 3 (descartadas)

- Binarios/sesiones viejos: **resuelto**, todo corre `527895b57`.
- Entrega servidor→cliente: funciona (`SidePaneImages: appended ... total=8`).
- Config: `pin_images = true`, sin toggles que oculten imágenes.
- Guard de sesión en `SidePaneImages` (session_id mismatch → ignora): no aplica,
  el log muestra `appended`.

### Causa 4 (secundarias)

- Si el usuario ve **bloques feos** en vez de "nada": es el fallback Halfblocks
  funcionando; "no se ven" = se ven degradadas.
- Si ve label + hueco en blanco: fallo de materialización (sin warnings en log,
  improbable).
- `JCODE_MERMAID_PICKER_PROBE=1` solo ayudaría si Herdr retransmite el query y los
  APC al terminal externo capaz (Ghostty sí soporta Kitty). Con Herdr 0.8.0 es
  dudoso según el handoff ("consume APC sin placement"), pero es la prueba más
  barata: lanzar el cliente con esa env y generar una imagen.
- tmux anidado dentro del pane añadiría otra capa de enmascaramiento (no es el caso
  actual según env de los procesos).

## 4. Fix propuesto

**Objetivo**: en el renderer inline del TUI, cuando `multiplexer=herdr` y hay
`HERDR_SOCKET_PATH` + `HERDR_PANE_ID`, dibujar las imágenes vía socket Herdr
(`pane.graphics.set`) en vez de APC/halfblocks.

Puntos de intervención:

1. **Detección** — `crates/jcode-tui-mermaid/src/mermaid_runtime.rs`:
   - Añadir un modo de protocolo "Herdr" (o flag paralelo al `Picker`) activo cuando
     `detect_multiplexer_from_env() == Multiplexer::Herdr` y existen
     `HERDR_SOCKET_PATH`/`HERDR_PANE_ID` no vacíos.
   - Reusar la lógica de `jcode-terminal-image::display_herdr`
     (`crates/jcode-terminal-image/src/display.rs:252`): JSON-RPC
     `{method: "pane.graphics.set", params: {pane_id, format, data_base64,
     image_width, image_height}}` por `UnixStream`, timeout 2 s.
   - Ideal: extraer el cliente del socket a un crate compartido
     (`jcode-terminal-image` ya es dependencia ligera; el TUI puede usarlo
     directamente) en vez de duplicar código.

2. **Draw step** — `crates/jcode-tui/src/tui/ui_viewport.rs` (rama `is_fit`,
   ~L998): hoy llama a `mermaid::render_image_widget_fit_stable` (ratatui-image).
   En modo Herdr, sustituir por una llamada que:
   - materialice la imagen (ya existe: `ensure_drawable` → cache en disco/memoria);
   - envíe `pane.graphics.set` con el PNG cacheado;
   - pinte las filas placeholder vacías (Herdr dibuja encima usando el cursor).
   - Ojo: `pane.graphics.set` usa el cursor actual del pane; en una TUI ratatui a
     pantalla completa el cursor está en el input. Puede requerir posicionar cursor
     o que Herdr soporte coordenadas explícitas — **verificar capacidades de la API
     en Herdr 0.8.0 antes de implementar** (quizá acepte `row`/`col` opcionales;
     el handoff anterior no lo exploró).

3. **Alternativa barata de validar primero**: probar `JCODE_MERMAID_PICKER_PROBE=1`
   en un pane Herdr y generar una imagen. Si Herdr ≥ alguna versión pasa Kitty al
   outer terminal, el picker detectaría Kitty y las imágenes se verían nítidas sin
   tocar código. Si no se ve nada, confirma que Herdr sigue tragándose los APC y
   que el fix (1)+(2) es el camino.

4. **Secundario (camino `read`)**: si se quiere que `display_image` funcione en
   sesiones TUI, habría que ejecutar el display en el cliente (el daemon no tiene
   TTY ni env Herdr). Hoy el transcript inline ya cubre las imágenes del `read`
   vía `with_labeled_image` → mismo renderer del punto 2. Probablemente basta con
   arreglar el renderer inline y dejar `display_image` solo para `jcode run`
   standalone con TTY.

## 5. Cómo verificar el fix

1. `cargo test -p jcode-tui-mermaid -p jcode-terminal-image` (detección).
2. Build selfdev: `selfdev build-reload` o
   `scripts/dev_cargo.sh build --profile selfdev -p jcode --bin jcode`.
3. Lanzar un **cliente nuevo** en un pane Herdr (no reutilizar sesiones abiertas).
4. Generar una imagen (p. ej. pedir al agente una imagen con gpt-image) y
   comprobar que aparece inline y nítida en el transcript.
5. Comprobar en el log (`~/.jcode/logs/jcode-<fecha>.log`) que el picker init
   reporta el modo Herdr nuevo en vez de `env_protocol=None`.
6. Probe manual del socket (ya usado en el handoff anterior): enviar
   `pane.graphics.set` con un PNG pequeño a
   `/Users/ferrer/.config/herdr/sessions/repo-term/herdr.sock` y confirmar que
   Herdr lo pinta en el pane `HERDR_PANE_ID`.

## 6. Archivos clave

- `crates/jcode-terminal-image/src/display.rs` — `ImageProtocol::Herdr`,
  `display_herdr` (cliente socket), `can_display_to_stdout`.
- `crates/jcode-tui-mermaid/src/mermaid_runtime.rs` — `Multiplexer::Herdr`,
  `infer_protocol_from_env`, `fast_picker`, `decide_picker_init_mode`,
  `native_image_protocol_available`, `uses_text_image_fallback`.
- `crates/jcode-tui/src/tui/ui_inline_image.rs` — sección inline, materialización,
  prewarm, `AnchoredInlineImages`.
- `crates/jcode-tui/src/tui/ui_viewport.rs` (~L923-1020) — draw de
  `ImageRegionRender::Fit`, llama a `render_image_widget_fit_stable`.
- `crates/jcode-app-core/src/tool/read.rs:346` — `handle_image_file` (camino
  daemon, `display_image`).
- `crates/jcode-app-core/src/agent/turn_streaming_mpsc.rs` (~L640-677) — emite
  `ServerEvent::GeneratedImage` + `ServerEvent::SidePaneImages` (esto ya funciona).
- `crates/jcode-tui/src/tui/app/remote/server_event_handlers.rs:59` —
  `handle_generated_image` (cliente remoto).
- Logs: `~/.jcode/logs/jcode-<YYYY-MM-DD>.log` (buscar `Mermaid picker init`,
  `SidePaneImages`).
