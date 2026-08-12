# graft-mem-vault — Vault de Obsidian uniendo estructura de código e historia de sesiones

Genera un vault de Obsidian que cruza **el grafo de código de [graft](https://github.com/parcadei/graft)** con **la memoria de sesiones de claude-mem**. Cada fichero se convierte en una nota con sus símbolos, sus llamadas entrantes y salientes, y **las observaciones de claude-mem que lo tocaron**.

Entiende el paradigma **HUB+SPOKE**: descubre solo los repos vinculados y agrega todos sus índices en un único grafo. Incorpora también los documentos markdown del proyecto —handoffs, ledgers, tracking, planes, `CLAUDE.md`— que graft no indexa pero son entre el 8 % y el 35 % de las rutas que registra claude-mem.

## Por qué

Los dos sistemas tienen mitades complementarias del mismo conocimiento, y ninguno tiene la otra:

| Sistema | Sabe | No sabe |
|---------|------|---------|
| **graft** | Qué existe, qué llama a qué, en qué línea | Por qué está así, qué se rompió, qué se decidió |
| **claude-mem** | Qué se investigó, qué bug apareció, qué se descartó | Dónde vive el código, quién depende de quién |

La junta es que `observations.files_modified` y `observations.files_read` guardan rutas de fichero, y los nodos de graft están indexados por ruta.

Ejemplo real (EVOTEKNIC, Odoo): la nota de `account_payment_line.py` reúne 17 símbolos con su span exacto **y** 128 observaciones acumuladas, entre ellas «`gastos_devolucion` tiene solo 5 referencias — confirmado metadato puro, no lógica contable». Eso es conocimiento institucional pegado al fichero.

## HUB+SPOKE

En este flujo de trabajo la sesión se lanza desde el **hub** del proyecto (`~/ai/HUB/<PROYECTO>`) pero el código suele vivir en **spokes**: repos vinculados que se abren según necesidad. El hub concentra el foco inicial; los spokes son el código real.

Eso rompe el cruce ingenuo: `graft build` se ejecuta en el hub, pero claude-mem registra rutas de los spokes. MYC18 es el caso de libro — el hub tiene 12 ficheros indexados mientras el código vive en tres spokes distintos.

**El vínculo no está declarado en ninguna parte** (no hay symlinks ni manifiesto), así que se descubre de la propia claude-mem: cada ruta absoluta observada se remonta hasta su raíz git, y las raíces con suficientes referencias son los spokes del proyecto. Después se agregan todos los `wiring.json` disponibles.

Efecto medido en MYC18: de 11 a **454 ficheros con memoria asociada**, y de 335 a **918 observaciones** enganchadas.

### Fusión hub/spoke

Un mismo fichero visto desde el hub y desde un spoke es el mismo código en dos sitios, así que **siempre se fusiona en una sola nota** por ruta relativa. Cuando los checkouts han divergido (distinto `body_hash`), la nota lo avisa y registra `variants: N` en el frontmatter; los símbolos listados son la unión de las versiones.

Separarlos sería peor por partida doble: partiría la historia del fichero en dos y, al crear dos claves para la misma ruta, volvería ambigua esa ruta y desactivaría las capas 2 y 3 del matcher. Se midió: al indexar el spoke de EVOTEKNIC, separar los checkouts divergidos hundía el enganche de 735 a 369 observaciones; fusionándolos sube a 745.

## Dependencias

| | qué | para qué | si falta |
|---|---|---|---|
| **obligatoria** | Python 3 | todo; solo biblioteca estándar, sin `pip install` | no instala |
| **obligatoria** | [graft](https://github.com/parcadei/graft) | el grafo de código (`graft build` en cada raíz) | no instala |
| **obligatoria** | [claude-mem](https://github.com/thedotmack/claude-mem) | las observaciones, en `~/.claude-mem/claude-mem.db` | no instala |
| **obligatoria** | `git` | descubrir spokes (`.git`), documentos (`git ls-files`) y commits (`git log`) | no instala |
| **obligatoria** | Claude Code o Codex | destino de scripts, regla y hooks | no instala |
| recomendada | `sqlite3` con FTS5 (el de Python suele traerlo) | búsqueda por contenido, 4 ms en vez de 225 | el resto funciona; sin búsqueda conceptual |
| opcional | [Obsidian](https://obsidian.md) | leer y navegar el vault | los vaults se generan igual; se leen con cualquier editor |
| opcional | `ripgrep` | búsqueda en documentos | usa `grep`, más lento |
| opcional | `sqlite3` CLI | el instalador lista los proyectos | solo cosmético |

`bash install.sh --check` las comprueba todas sin instalar nada.

**No hace falta ningún MCP server ni tener Obsidian abierto.** El vault son ficheros de texto plano; la alternativa habitual (plugin Local REST API + `mcp-obsidian`) exige la aplicación corriendo durante toda la sesión y no aporta capacidad que Claude Code no tenga ya.

## Instalación

```bash
bash install.sh              # comprueba dependencias e instala
bash install.sh --check      # solo comprueba
bash install.sh --no-hooks   # scripts y regla, sin tocar settings.json
bash install-codex.sh        # instala vault + ingesta nativa de claude-mem en Codex
bash install-codex.sh --check # solo comprueba dependencias para Codex
```

Es idempotente: reejecutarlo no duplica hooks ni pisa la configuración.

## Consulta barata: vault_scout

Una consulta exploratoria contra `vault_lookup.py` — «¿ya intentamos arreglar el diario de cobro?» — costaba de media **~6.000 tokens** de observaciones crudas para llegar a una respuesta que, resumida, cabe en ~400. `vault_scout.py` cierra esa distancia sin cambiar el contrato: recolecta, recorta y devuelve hechos anclados a su fuente en vez de volcar el material entero.

El flujo tiene tres pasos:

1. **Recolección determinista** — el mismo FTS5 de `vault_lookup.py`, sin modelo de por medio, más un barrido de `thoughts/shared/handoffs/` y `thoughts/ledgers/` en el repo actual: el vault indexa markdown con `git ls-files '*.md'`, así que un handoff recién escrito y sin commitear le es invisible, y ese barrido es lo único que lo encuentra.
2. **Recorte determinista** — se queda con título, resumen y hechos de cada observación y descarta la prosa redundante. Aquí está el **90 % del ahorro**, y el riesgo es cero porque es recorte, no redacción: no puede inventar nada que no estuviera ya en la nota.
3. **Modelo local en modo extractivo (opcional)** — solo si el interruptor está encendido, sintetiza un veredicto citando cada afirmación con su referencia (`[obs 1]`, `[ses-sesion-a]`…). Si el veredicto no supera la validación de referencias, se entrega el extracto determinista del paso 2 en su lugar.

**Apagado por defecto.** Se enciende con:

```bash
touch ~/.claude/.vault-scout-on
```

Con el interruptor apagado, la salida es **exactamente** la de `vault_lookup.py`, sin ninguna diferencia — comprobado byte a byte con `diff`.

```bash
python3 ~/.claude/scripts/vault_scout.py "<pregunta>"     # veredicto con ids
python3 ~/.claude/scripts/vault_scout.py --dry-run "<p>"  # sin llamar a ningún modelo
python3 ~/.claude/scripts/vault_scout.py --check          # interruptor y motor
```

`--check` es la excepción a esa paridad: informa del estado del interruptor y del motor con `rc=0` **aunque el interruptor esté apagado** — es su razón de ser, no un passthrough.

### Descableado en tres niveles

| Nivel | Cómo | Efecto |
|---|---|---|
| Apagar | no tocar nada, o borrar `~/.claude/.vault-scout-on` | el scout sigue instalado pero delega en `vault_lookup.py` sin diferencias |
| Desinstalar el scout | `bash install.sh --quitar-scout` | borra `vault_scout.py`, el interruptor `~/.claude/.vault-scout-on` y su `__pycache__`, y revierte el bloque `<!-- vault-scout -->` de la regla; el resto del trick sigue funcionando |
| A mano | `rm ~/.claude/scripts/vault_scout.py` y borrar el bloque entre `<!-- vault-scout -->` y `<!-- /vault-scout -->` en `~/.claude/rules/vault-lookup.md` | mismo resultado que `--quitar-scout`, sin pasar por el instalador |

`--quitar-scout` cuenta las marcas antes de tocar la regla: si `~/.claude/rules/vault-lookup.md` se editó a mano y quedó con una marca de apertura sin su cierre (o viceversa), no toca el fichero — un `sed` sobre un rango sin cierre borraría hasta el final — y avisa por stderr de que hay que quitar el bloque a mano entre las marcas. El script se borra igual en ese caso.

### Límite declarado

La validación de referencias detecta ids fabricados: si el veredicto cita `[obs 999]` y esa referencia no viajó en el corpus, se rechaza. Lo que **no** detecta es la deriva de atribución — fundir dos hechos ciertos de fuentes distintas en una frase que ninguna de las dos sostiene por sí sola. Contra eso la única defensa es de formato: el prompt exige una fuente por línea, no una validación semántica.

Probado de punta a punta contra el vault real de este repositorio y contra `ollama run qwen3.5:4b-nvfp4` en local. El modelo resultó ser el eslabón más débil: vuelca razonamiento y secuencias de escape ANSI en la salida, así que hubo que aislar el bloque del veredicto antes de validarlo.

Diseño completo: [`docs/superpowers/specs/2026-08-08-vault-scout-design.md`](../../docs/superpowers/specs/2026-08-08-vault-scout-design.md).

### Qué instala y dónde

| destino | contenido |
|---|---|
| `~/.claude/scripts/` | `graft_mem_vault.py`, `refresh_vaults.py`, `vault_lookup.py`, `vault_scout.py` |
| `~/.claude/rules/` | `vault-lookup.md` — regla global |
| `~/.claude/hooks/` | `vault-session-start.py`, `vault-read-nudge.py` |
| `~/.claude/settings.json` | 3 hooks registrados (copia previa en `.bak-<epoch>`) |
| `~/.codex/graft-mem-vault/` | scripts y hooks usados por Codex |
| `~/.codex/hooks.json` | hooks del vault y hooks nativos de `claude-mem` para Codex (copia previa en `.bak-<epoch>`) |
| `~/.jcode/graft-mem-vault/scripts/` | scripts usados por jcode; el MCP intenta esta ruta y cae a `~/.claude/scripts/` si todavía no existe |
| `~/.config/graft-mem-vault/projects.json` | configuración de proyectos (la crea el primer `refresh_vaults.py`) |
| `~/vaults/<PROYECTO>-graft-mem/` | los vaults generados |

Nada de esto vive en el repo del proyecto: un vault es una caché derivada, se regenera entero y no se versiona.

### Portabilidad

Todas las rutas se pueden mover por entorno, para otra máquina con otra distribución de directorios:

| variable | por defecto |
|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` |
| `CODEX_HOME` | `~/.codex` |
| `CLAUDE_MEM_DB` | `~/.claude-mem/claude-mem.db` |
| `CLAUDE_MEM_CODEX_HOOKS` | autodetecta el `hooks/codex-hooks.json` activo en la caché de Claude Code o de Codex; permite forzar una ruta concreta |
| `GRAFT_MEM_VAULT_CONFIG` | `~/.config/graft-mem-vault/projects.json` |
| `GRAFT_MEM_VAULT_HUBS` | `~/ai/HUB` (dónde buscar proyectos al autodescubrir) |
| `GRAFT_MEM_VAULT_MIN_OBS` | `80` (mínimo de observaciones para generar vault) |
| `OBSIDIAN_CONFIG` | se detecta solo: macOS, Linux o `%APPDATA%` |
| `VAULT_SCOUT_INTERRUPTOR` | `~/.claude/.vault-scout-on` (ruta del fichero del interruptor) |
| `VAULT_SCOUT_ON` | apagado (`1`/`true`/`yes` lo enciende sin tocar el fichero) |
| `VAULT_SCOUT_MODEL` | `qwen3.5:4b-nvfp4` (`none` desactiva el motor) |
| `VAULT_SCOUT_TIMEOUT` | `60` (segundos por motor; un valor inválido cae al defecto) |
| `FCC_URL` | `http://127.0.0.1:8082` (motor de respaldo del scout) |
| `FCC_AUTH_TOKEN` | `freecc` |

El registro de Obsidian se busca en las tres rutas de plataforma, y la detección de «Obsidian abierto» prueba `Obsidian` y `obsidian` porque el binario se llama distinto en macOS y en Linux.

## Uso

```bash
python3 ~/.claude/scripts/graft_mem_vault.py <hub> <proyecto-claude-mem> <dir-vault>
```

```bash
python3 ~/.claude/scripts/graft_mem_vault.py \
    ~/ai/HUB/MYC18 MYC18 ~/vaults/MYC18-graft-mem
```

El segundo argumento es el valor de la columna `project` en la base de datos, que no siempre coincide con el nombre del directorio. Admite **varios nombres separados por comas**. Para verlos:

```bash
sqlite3 -readonly ~/.claude-mem/claude-mem.db \
  "SELECT project, count(*) FROM observations GROUP BY project ORDER BY 2 DESC LIMIT 20;"
```

Opciones:

| Opción | Por defecto | Qué hace |
|--------|-------------|----------|
| `--min-refs=N` | 5 | Referencias mínimas para admitir una raíz git como spoke. Bájalo si falta un spoke poco tocado; súbelo si se cuelan repos ajenos |
| `--max-files=N` | 5000 | Descarta índices por encima de N ficheros (árbol vendorizado). Súbelo si tu repo es legítimamente grande |
| `--root=RUTA` | — | Añade una raíz explícita, aunque no sea raíz git. Repetible. Para acotar un repo cuyo índice completo sería inservible |
| `--no-docs` | — | Omite los documentos y la configuración; solo grafo de código |
| `--no-commits` | — | Omite los mensajes de commit |
| `--no-sessions` | — | Omite las notas de sesión |
| `--build` | — | Ejecuta `graft build` en cada raíz antes de generar |

Después, en Obsidian: **«Abrir carpeta como vault»** y selecciona el directorio. El URI `obsidian://open?path=…` solo funciona con vaults ya registrados, así que la primera vez hay que abrirlo a mano.

## Qué genera

```
vault/
├── INDEX.md           raíces (hub + spokes), sesiones recientes, ranking, puntos ciegos
├── code/     N notas  una por fichero: símbolos con file:line, llamadas, memoria, commits
├── docs/     D notas  una por documento o config: contenido completo, ficheros citados
├── memory/   M notas  una por observación: narrativa, hechos, conceptos, ficheros, sesión
├── sessions/ S notas  una por sesión: petición, investigado, aprendido, qué quedó abierto
└── commits/  C notas  una por commit: asunto, cuerpo, autor, ficheros tocados
```

Las cinco categorías se reparten dos preguntas distintas. Código, documento y commit responden al **qué** (qué fichero, qué cambio); la observación y la sesión responden al **por qué**, que es lo que no se recupera releyendo el código.

Los enlaces son `[[wikilinks]]` bidireccionales, así que la vista de grafo de Obsidian muestra las dos redes fundidas.

El `INDEX.md` incluye:

- **Tabla de raíces** con sus referencias y estado (`✓` indexada, `✗` sin indexar, `⚠` descartada), y los comandos `graft build` que faltan por ejecutar.
- **Sesiones recientes**: las diez últimas, con su fecha, cuántas observaciones dejaron y la petición que las arrancó.
- **Ficheros con más historia**, ordenados por observaciones acumuladas.
- **Puntos ciegos**: ficheros que graft conoce pero sobre los que no hay ninguna observación. Es código nunca tocado con Claude, y saberlo es información en sí misma.

## Documentos

graft solo parsea código, pero la capa de plan y seguimiento de un proyecto vive en markdown. Se recogen los `.md` **trackeados en git** de cada raíz (`git ls-files` deja fuera de un plumazo las cachés de herramientas, que están gitignoradas) y se genera una nota por documento con:

- **El contenido completo embebido.** El vault se regenera entero en cada pasada, así que no hay riesgo de divergencia, y un handoff a medias no sirve de nada.
- **Las observaciones que lo tocaron**, igual que con el código.
- **`## Ficheros que cita`**: las rutas de código o de configuración mencionadas en la prosa se resuelven con el mismo matcher de tres capas y se convierten en wikilinks. Eso cierra el triángulo **plan ↔ código ↔ incidencias**: un plan de migración que nombra `models/sale_order.py` enlaza con esa nota y con su historial de incidencias.

Se incluye `CLAUDE.md`: tiene 161 referencias en IDRUS-MIG y 42 en KAREY, y explica por qué el agente se comporta como se comporta en ese repo.

Impacto medido al incorporarlos:

| proyecto | antes | después |
|---|---|---|
| RAPIT-PAQUET | 422 | **854** (el 35 % de sus rutas son `.md`) |
| MYC18 | 918 | **1.211** |
| IDRUS-MIG | 525 | **822** |
| KAREY | 2 | **121** (su hub no tiene código, solo documentos) |
| CURSO | 13 | **74** |

### Configuración: la capa de decisión

El markdown es la capa de plan, pero la decisión suele estar escrita en un `.yaml`. Los handoffs de estos repos se guardan en yaml, los perfiles del instalador son yaml, el wiring de los MCP es json. Nada de eso tenía categoría en el vault: `git ls-files "*.md"` los dejaba fuera, y eran justo los ficheros que explican por qué el sistema está montado así.

Qué entra ahora, además del markdown:

| Entra | No entra | Por qué |
|-------|----------|---------|
| `*.yaml`, `*.yml`, `*.toml` | nombres con `lock`, `secret`, `credential`, `.env` | El vault es texto plano y se comparte |
| `.claude/*.json`, `.codex/*.json` | cualquier otro `.json` | En la config de agentes el json es decisión (settings, hooks, mcp); fuera es lockfile o datos volcados |
| — | `settings.local.json` | Es donde acaban las variables de entorno con credenciales |

El filtro por nombre **solo se aplica a la configuración, nunca al markdown**: un `tricks_ferrer/token-optimizer/README.md` no es un secreto, y filtrar por palabra en los `.md` perdería documentos legítimos.

El contenido no markdown se embebe dentro de un bloque con su lenguaje (` ```yaml `), para que Obsidian no lo interprete y para que el yaml siga leyéndose como yaml. Las comillas triples del propio contenido se neutralizan para que no cierren el bloque antes de tiempo. La nota declara `doc_kind` en el frontmatter, así que se puede filtrar por tipo desde Obsidian.

En este repo (`guia`) son 208 ficheros de configuración trackeados frente a 831 markdown, incluidos todos los handoffs en yaml y `instalador/catalogo.yaml`.

Con `--no-docs` se genera solo el grafo de código.

## Sesiones

Es la única capa cuyo contenido **ya venía escrito y no se usaba**. claude-mem redacta un resumen por prompt en su tabla `session_summaries` —qué se pidió, qué se investigó, qué se aprendió, qué se completó, qué queda abierto— y nada lo leía: 5.423 filas en esta máquina.

El problema que resuelve: en `guia` había 3.921 observaciones repartidas en 180 sesiones, y ningún nodo que dijera «estas dieciocho observaciones son una sola sesión, y lo que se pedía era esto». El vault sabía qué se tocó, pero no qué se estaba intentando.

Cada nota de sesión lleva:

- **Un bloque por prompt**, en orden, con los campos que claude-mem rellenó. Leídos en fila, cuentan la sesión.
- **`## Observaciones`**: todas las de esa tanda, con su título.
- **`## Ficheros`**: la unión de lo que la sesión tocó.
- El salto es bidireccional: cada nota de memoria lleva `## Sesión` con el enlace de vuelta y `session:` en el frontmatter.

Solo se genera nota para las sesiones que dejaron **alguna observación enganchada al vault**: una sesión cuyo trabajo no tocó este proyecto no tiene de dónde colgarse. Y una sesión con observaciones pero sin resumen genera nota igual, avisando de que no hay resumen: la agrupación ya vale por sí sola.

Lo que **no** se incorpora, aunque esté en la misma base: la tabla `user_prompts` (8.907 filas). Un prompt sin su respuesta es ruido, y es exactamente donde se pegan rutas, tokens y credenciales. El campo `request` del resumen da la misma intención ya destilada. Con `--no-sessions` se omite la categoría.

## Refrescar todos los vaults

`refresh_vaults.py` regenera todos los proyectos de una pasada y los da de alta en Obsidian:

```bash
python3 refresh_vaults.py                 # refresca todo y registra en Obsidian
python3 refresh_vaults.py --only MYC18    # solo un proyecto
python3 refresh_vaults.py --register-only # solo el alta en Obsidian
python3 refresh_vaults.py --dry-run       # enseña los comandos sin ejecutarlos
```

La configuración vive en `~/.config/graft-mem-vault/projects.json`. Si no existe se genera por descubrimiento —proyectos de claude-mem con hub en `~/ai/HUB` y al menos 80 observaciones— y se deja escrita para editarla. El descubrimiento solo crea el fichero inicial: cuando la configuración ya existe, un proyecto nuevo se añade a mano para no reescribir decisiones como `roots`, `also`, `min_refs` o `no_build`. Ahí es donde se declaran de una vez las raíces acotadas de un proyecto, en vez de repetir once `--root` en cada invocación:

```json
{
  "vault_dir": "~/vaults",
  "projects": {
    "MYC18": { "hub": "~/ai/HUB/MYC18", "roots": [] },
    "IDRUS-MIG": {
      "hub": "~/ai/HUB/IDRUS-MIG",
      "roots": ["~/ai/HUB/IDRUS-MIG/scripts", "~/ai/HUB/IDRUS-MIG/docs"],
      "min_refs": 5
    }
  }
}
```

### Alta automática en Obsidian

El registro de vaults de Obsidian es `~/Library/Application Support/obsidian/obsidian.json`. `refresh_vaults.py` añade ahí los vaults generados —solo añade, nunca borra, y deja copia en `.bak`—, así que no hay que hacer «Abrir carpeta como vault» a mano.

**Obsidian tiene que estar cerrado**: reescribe ese fichero al salir y se perdería el alta. Si está abierto, el script lo detecta, no toca nada y te dice que lo cierres y ejecutes `--register-only`.

## Consultarlo desde una sesión

El vault no sirve de nada si hay que acordarse de mirarlo. Tres mecanismos, de menos a más automático:

### 1. `vault_lookup.py` — la consulta barata

**No hay que decirle de qué tipo es la consulta.** El término se clasifica solo:

```bash
vault_lookup.py models/sale_order.py   # ruta o nombre  → ficha del fichero
vault_lookup.py 29052                  # solo dígitos   → abre esa observación
vault_lookup.py gastos_devolucion      # concepto       → busca en el contenido
vault_lookup.py "efectos devueltos"    # frase          → busca en el contenido
```

Si buscas por nombre y no hay fichero, cae automáticamente a búsqueda por contenido. Los flags (`--obs`, `--ses`, `--doc`, `--name`, `--text`, `--stats`, `--all`) siguen ahí solo por si quieres forzar.

Una búsqueda por concepto devuelve las tres capas de una vez: las observaciones (qué se tocó), **las sesiones que ya trabajaron eso** (qué se pretendía) y los documentos que lo mencionan. La sesión se abre entera con `--ses <id>`:

```bash
vault_lookup.py conciliacion            # observaciones + sesiones + documentos
vault_lookup.py --ses 6028e9ea-5ea3-…   # la sesión completa: petición y aprendizajes
```

Las sesiones se buscan contra `session_summaries_fts`, que claude-mem también mantiene, y se deduplican por sesión: hay un resumen por prompt, así que la misma sesión aparecería varias veces.

Por defecto la búsqueda es del vault del repo actual. Con `--global` cruza **todos** los vaults, etiquetando cada acierto con su proyecto:

```bash
vault_lookup.py --global gastos_devolucion         # en todos los proyectos, no solo este
vault_lookup.py --global --obs MYC18:29052         # abre una observacion de otro proyecto
```

Pensado para dos disparadores: lo pide el usuario, o el modelo detecta que la pregunta excede el repo actual (arquitectura de una herramienta compartida, "¿ya resolvimos esto en otro proyecto?") y lo lanza él mismo.

Resuelve solo el proyecto desde el directorio actual —la raíz más específica que lo contenga gana, comparando **rutas reales**, porque en macOS `/var` es un enlace a `/private/var` y `~/.claude` suele serlo a `~/.claude`— y sale limpio si no pertenece a ninguno.

#### Búsqueda por contenido: FTS5, no grep

Las observaciones se buscan con la tabla `observations_fts` que **claude-mem ya mantiene**. Medido sobre 136 MB de vaults:

| método | tiempo | ranking |
|---|---|---|
| `grep -r` | 225 ms | no |
| `rg` | 179 ms | no |
| **FTS5 de claude-mem** | **4 ms** | **sí** |

56× más rápido, y sobre todo **ordenado por relevancia**: `grep` devuelve 29 ficheros sin orden y te toca leerlos. Los documentos `.md` no están en FTS5, así que ahí sí se usa `rg` (con `grep` de reserva si no está instalado).

Se descartan los ids que no tienen nota en el vault: una observación consolidada o archivada por **memory-engineering** sigue en el índice pero ya no tiene nota, y enlazarla sería mentir.

Está pensado en **dos pasos**, y la diferencia es grande: el índice de `account_payment_line.py` cuesta ~450 tokens y lista los títulos de sus 128 observaciones; volcarlas todas cuesta ~9.400. Primero el índice, después solo lo que interese.

### 2. Regla global

`rules/vault-lookup.md` se instala en `~/.claude/rules/` y le dice al modelo cuándo consultar el vault y cómo hacerlo sin quemar contexto. Es global, no por repo: como el script detecta solo si hay vault, una sola regla vale para todos los proyectos.

### 3. Hooks — que la consulta se instigue sola

| hook | evento | qué hace |
|------|--------|----------|
| `vault-session-start.py` | `SessionStart` (`startup\|resume\|compact\|clear`) | Inyecta las cifras del vault y los ficheros con más historia. **Y lanza el refresco en segundo plano.** |
| `vault-read-nudge.py` | `PreToolUse` (`Read`) | Si el fichero que se va a leer tiene histórico, inyecta el recuento y los tres títulos más recientes |
| `vault-prompt-search.py` | `UserPromptSubmit` | Busca la pregunta en el vault **antes de que el modelo elija herramienta** e inyecta los aciertos. Si no hay nada en este repo pero sí en otros proyectos, avisa de eso en vez de callar — la señal para probar `--global` |

El nudge de lectura avisa al abrir un fichero: el modelo se entera de que hay 98 observaciones sobre él antes de empezar. No bloquea nunca la lectura y no repite el aviso del mismo fichero en 30 minutos.

Pero el que cierra el círculo es el de `UserPromptSubmit`, porque **el primer movimiento ante una pregunta rara vez es leer un fichero**: es buscar, o razonar. Y una pregunta conceptual («¿por qué falla la conciliación de efectos devueltos?») no toca ningún fichero, así que sin él el histórico no aparecía nunca salvo que alguien se acordara de pedirlo. Va contra el índice FTS5, así que cuesta milisegundos, y calla salvo que haya al menos dos aciertos: un resultado suelto es más ruido que ayuda. Ejemplo real, sin nombrar ningún fichero:

```
[vault] El historico de EVOTEKNIC tiene material sobre esto. Miralo antes de investigar desde cero:
  29104  2026-07-31  Bug identificado: RegistrarCobro usaba el diario incorrecto por busqueda por tipo
  29043  2026-07-31  Efectos Comerciales Reconciliation Flow Map y gastos_devolucion Field Location
  29105  2026-07-31  Conciliaciones cruzadas en produccion: B260099 y B260087
```

Sigue habiendo un hueco consciente: un `Grep` o un `graft ask` que no venga precedido de una pregunta no dispara nada.

El refresco en `SessionStart` va **desprendido**, no en primer plano: IDRUS-MIG tarda 24 s y agotaría el timeout del hook. Las cifras que se inyectan son las del vault actual y la sesión siguiente verá el refrescado; da igual, porque el valor del vault es el histórico, no lo de hace treinta segundos. Un fichero de lock evita apilar refrescos si se encadenan varios `/clear`.

Los hooks son **fail-open por construcción**: ante stdin vacío, JSON roto, rutas inexistentes o un directorio sin vault, emiten `{}` y salen con 0. `self_check_hooks.py` lo verifica sobre entradas malformadas, porque un hook que revienta bloquea la sesión entera.

### 4. Configuración para Codex

`install-codex.sh` convierte Codex en una fuente de memoria de primera clase. Reutiliza **la misma** base `~/.claude-mem/claude-mem.db`, los mismos índices de graft y los mismos vaults que Claude Code; no crea una memoria paralela.

El instalador localiza el `hooks/codex-hooks.json` de la versión activa de `claude-mem` y fusiona sus definiciones en `~/.codex/hooks.json`, junto con los cuatro hooks propios del vault. El merge es idempotente: sustituye solo una integración nativa anterior de `claude-mem`, conserva cualquier hook ajeno y crea una copia `.bak-<epoch>` antes de escribir. En `SessionStart` invoca directamente el handler `context`, para que un marcador opcional `.install-version` ausente no bloquee la inyección cuando el worker ya está operativo.

| evento Codex | handler de `claude-mem` | resultado |
|---|---|---|
| `SessionStart` (`startup\|resume\|compact\|clear`) | `context` | inyecta las observaciones y resúmenes recientes al abrir, reanudar, compactar o ejecutar `/clear` |
| `UserPromptSubmit` | `session-init` | alinea la sesión y registra el prompt |
| `PreToolUse` (`Bash` y lecturas MCP) | `file-context` | aporta memoria del fichero antes de usarlo |
| `PostToolUse` (todas las herramientas locales) | `observation` | convierte el trabajo de Codex en observaciones |
| `Stop` | `summarize` | cierra el turno con su resumen de sesión |

El ciclo alrededor de `/clear` tiene dos velocidades deliberadas:

1. `claude-mem` lee e inyecta inmediatamente desde la base común, y `vault-prompt-search.py` consulta ese mismo FTS5 en cada prompt. Las observaciones nuevas se pueden encontrar aunque Obsidian todavía no se haya regenerado.
2. `vault-session-start.py` reconstruye graft y el vault en segundo plano. Los hooks coincidentes de Codex se lanzan en paralelo, y proyectos grandes como IDRUS-MIG tardan unos 24 s; bloquear `/clear` hasta terminar empeoraría cada reinicio sin aportar memoria adicional al modelo.

No actives a la vez `CLAUDE_MEM_CODEX_TRANSCRIPT_INGESTION=true`: el propio `claude-mem` considera autoritativos sus hooks nativos de Codex. El watcher de `~/.codex/sessions/**/*.jsonl` queda como alternativa de compatibilidad, no como segundo camino de ingesta, porque ambos juntos podrían duplicar observaciones.

Después de instalar o actualizar, reinicia Codex y abre `/hooks`: los comandos nuevos o modificados se ejecutan solo después de revisar y confiar en su hash. Si actualizas `claude-mem`, vuelve a ejecutar `install-codex.sh` para importar las definiciones de esa versión.

#### Instalación reproducible en otro equipo

El instalador no descarga dependencias ni copia secretos. Parte de una instalación funcional de Python, git, graft y claude-mem, y construye encima la integración global de Codex:

```bash
# 1. Llevar el repositorio guia al equipo y entrar en el trick
cd ~/guia/tricks_ferrer/graft-mem-vault

# 2. Dependencia de grafo; claude-mem se instala desde su plugin oficial
npm i -g @parcadei/graft

# 3. Comprobar sin escribir y después instalar globalmente para Codex
bash install-codex.sh --check
bash install-codex.sh

# 4. Verificación local reproducible
python3 self_check_codex.py
```

Antes del paso 3 deben existir la base `~/.claude-mem/claude-mem.db` y los hooks nativos `hooks/codex-hooks.json` de claude-mem. El instalador los busca, por este orden, en una ruta explícita (`CLAUDE_MEM_CODEX_HOOKS`), en la caché de Claude Code (`$CLAUDE_CONFIG_DIR/plugins/cache/thedotmack/claude-mem/...`) y en la caché de Codex (`$CODEX_HOME/plugins/cache/*/claude-mem/...`). Esto permite instalar en un equipo con ambos harnesses o en uno donde claude-mem solo está desplegado como plugin de Codex. Las versiones marcadas como huérfanas se ignoran y, si hay varias, se escoge la más reciente.

Después se da de alta cada proyecto. La primera construcción de graft es explícita porque el refresco solo actualiza índices existentes:

```bash
# 5. Añadir el proyecto a ~/.config/graft-mem-vault/projects.json
cd ~/ai/HUB/MI-PROYECTO
graft build

# 6. Generar el vault y comprobar que el repo actual lo resuelve
python3 ~/.codex/graft-mem-vault/scripts/refresh_vaults.py --only MI-PROYECTO
python3 ~/.codex/graft-mem-vault/scripts/vault_lookup.py --stats
```

Si el repo contiene Odoo, OCA, `node_modules` u otros árboles vendorizados, no construyas el índice en la raíz: crea graft solo en los subdirectorios propios y decláralos en `roots`. Por último, reinicia Codex, aprueba los hashes en `/hooks` y prueba una sesión nueva y `/clear`. En ambos casos `SessionStart` debe mostrar el bloque de contexto de claude-mem; la regeneración del grafo y del vault seguirá en segundo plano.

La instalación y los checks se han verificado en macOS. El código contempla rutas de Obsidian de Linux y Windows, pero este montaje global de Codex no se ha validado de punta a punta en Windows; allí el instalador Bash necesita WSL o un entorno compatible.

#### Piezas portadas de Claude Code a Codex

| pieza | adaptación para Codex |
|---|---|
| `install-codex.sh` | instala en `$CODEX_HOME`, fusiona sin duplicar `hooks.json`, conserva hooks ajenos y crea `.bak-<epoch>` |
| hooks nativos de claude-mem | importa `context`, `session-init`, `file-context`, `observation` y `summarize` para que Codex escriba y lea la misma base |
| `vault-session-start.py` | acepta `startup`, `resume`, `compact` y `clear`; inyecta el estado actual y desprende el refresco largo |
| `vault-prompt-search.py` | consulta FTS5 antes de investigar y funciona igual desde ambos harnesses |
| `vault-read-nudge.py` | entiende rutas de `apply_patch`, `Edit`, `Write` y resultados MCP estructurados; en `PostToolUse` usa solo `systemMessage` |
| `graft_mem_vault.py`, `refresh_vaults.py`, `vault_lookup.py` | se copian sin bifurcar lógica a `~/.codex/graft-mem-vault/scripts/` y consumen la misma configuración, índices y vaults |
| `self_check_codex.py` | cubre extracción de rutas, fail-open, merge idempotente, caché exclusiva de Codex y contrato seguro de `PostToolUse` |

No se añadió un daemon ni una segunda base. El aparato común ya existía; la adaptación mínima fue transportar los eventos nativos de claude-mem a `~/.codex/hooks.json` y reutilizar los generadores.

#### Fixes y diagnóstico

- **`PostToolUse hook returned unsupported updatedMCPToolOutput`.** El nudge devolvía `hookSpecificOutput.additionalContext` después de `mcp__codebase_memory_mcp__get_code_snippet`. Aunque el contrato documenta contexto adicional, el harness normalizaba esa respuesta como el campo no soportado `updatedMCPToolOutput`. El fix es deliberadamente estrecho: en `PostToolUse` el script emite solo `systemMessage`; en los demás eventos conserva `additionalContext`. Hay una regresión sintética en `self_check_codex.py` que exige que ese campo no reaparezca.
- **El worker funciona pero `SessionStart` no inyecta contexto.** Algunas instalaciones no tienen el marcador opcional `.install-version`; el wrapper de comprobación de versión puede cortar antes de llamar al worker. El instalador reescribe solo el handler `context` para invocar directamente `bun-runner.js worker-service.cjs hook codex context`.
- **`claude-mem no aporta hooks/codex-hooks.json`.** En una máquina Codex-only el plugin puede no existir bajo `~/.claude`. El instalador busca también en `$CODEX_HOME/plugins/cache/*/claude-mem/*/`; el check portable reproduce exactamente ese escenario.
- **Observaciones duplicadas.** No actives `CLAUDE_MEM_CODEX_TRANSCRIPT_INGESTION=true` junto con estos hooks: la ingesta nativa ya es autoritativa.
- **Proyecto sin vault.** Añádelo a `projects.json`; el autodescubrimiento no vuelve a mezclar proyectos una vez creado el fichero.
- **Vault sin grafo.** Ejecuta `graft build` una vez en cada raíz elegida. `refresh_vaults.py` actualiza índices, pero no decide dónde crear uno nuevo.
- **Cambios de hooks que no se ejecutan.** Reinicia Codex y revisa `/hooks`; la confianza está ligada al hash del comando.

Todos los hooks propios son fail-open: con stdin vacío, JSON roto, una herramienta desconocida, una ruta inexistente o un repo sin vault devuelven `{}` y salen con código 0. La base y los vaults no se borran al reinstalar. Para deshacer solo la integración, restaura la copia más reciente `~/.codex/hooks.json.bak-<epoch>` y mueve `~/.codex/graft-mem-vault/` fuera de `$CODEX_HOME`; la memoria compartida queda intacta.


#### Instalación en jcode

jcode no inyecta contexto desde hooks: sus hooks son observadores `fire-and-forget` salvo `pre_tool`, que solo permite aprobar o denegar una herramienta. Por eso esta integración no intenta devolver `additionalContext` desde un hook y usa `.jcode/prompt-overlay.md`, que jcode lee al montar el prompt del proyecto. El puente escribe ahí primero el bloque del vault y después el de claude-mem.

```bash
cd ~/guia/tricks_ferrer/graft-mem-vault
bash install-jcode.sh
```

El instalador copia los scripts a `~/.jcode/graft-mem-vault/scripts/` y los hooks a `~/.jcode/graft-mem-vault/hooks/`, y cablea los eventos de jcode en `~/.jcode/config.toml`. El servidor MCP del vault se registra en el repo con `.mcp.json`; al ejecutarse, busca primero los scripts instalados en `~/.jcode/graft-mem-vault/scripts/` y usa `~/.claude/scripts/` como fallback para instalaciones antiguas o compartidas con Claude Code.

Queda un paso manual: envolver la función `jcode()` de `~/.zshrc` para llamar al puente en modo `--context` antes de lanzar el binario. Ese wrapper es deliberadamente manual porque `~/.zshrc` suele contener alias, rutas privadas y lógica local; el instalador no debe reescribirlo a ciegas.

`vault-read-nudge.py` y `vault-prompt-search.py` no se instalan como hooks propios en jcode, pero su cobertura sí está resuelta por otra vía. jcode admite un solo `pre_tool` y no tiene hook de envío de prompt, así que en vez de competir por ese hueco ambos se encadenan dentro del gate de tldr/rtk. El núcleo del nudge se expone en el puente como `vault_nudge()`: cuando una lectura grande ya iba a bloquearse, el bloqueo lleva el mapa estructural **y** el histórico del vault del fichero, sin gastar un turno extra.

El equivalente de `vault-prompt-search.py` (búsqueda por pregunta, no por fichero) se aproxima desde el mismo `pre_tool`. jcode no entrega el texto del prompt a ningún hook, así que la pregunta no se puede leer directamente: `turn_end` trae el texto del **asistente** y además llega tarde. Lo que sí es fiel es la *intención*, porque la primera herramienta de un turno lleva casi siempre los términos de lo que se acaba de pedir. El gate extrae esos términos de los argumentos de la primera herramienta del turno (`query` de grep, `command` de bash, ruta de read), consulta el FTS5 del vault igual que el hook original y, si hay material, bloquea una vez con el histórico. El marcador `~/.jcode/state/vault-turn-<sid>` garantiza **un solo aviso por turno**, y `--turn-end` lo borra al cerrar el turno.

Se mantienen los umbrales del hook original para no generar ruido: mínimo dos aciertos, texto de al menos 12 caracteres y una lista de palabras vacías. Saludos y confirmaciones («hola qué tal», «vale sigue») no disparan nada.

Un detalle que importa en un repo en español: el patrón `[A-Za-z_][A-Za-z0-9_.]{2,}` del hook original **parte las palabras por la tilde** («migración» → `migraci`, «diseñada» → `dise`), lo que degrada la búsqueda. Ambas versiones usan ya `[^\W\d_][\w.]{2,}` con `re.UNICODE`.

Este aviso cuesta un turno, a diferencia del nudge de lectura. Es el precio de no tener hook de prompt; el umbral de dos aciertos existe para que ese turno se gaste solo cuando el histórico tiene algo que decir.

**Refresco del contexto y `/clear`.** `session_start` solo se emite con `JCODE_HOOK_SOURCE` = `create`, `attach` o `resume`; `/clear` limpia el historial de la sesión viva y **no** lo reemite, así que no vuelve a ejecutarse el pipeline del vault. Lo que sí ocurre es que el overlay se relee en cada turno (verificado: cambiarlo a mitad de sesión cambia lo que ve el modelo), de modo que tras un `/clear` se reinyecta su contenido tal como esté en disco. Para que esa foto no envejezca, el hook `turn_end` llama al puente con `--turn-end`, que reescribe el overlay cuando lleva más de 10 minutos sin actualizarse. Con eso `/clear` recupera contexto reciente sin necesidad de reiniciar la sesión.

Si prefieres forzar el pipeline completo, el comando es `/fork` (o `/transfer`), que abre una sesión nueva y por tanto sí dispara `session_start` con `source=create`.

**No confundir con `jcode memory`.** jcode trae su propia memoria nativa, independiente de este aparato: la escribe el runtime del TUI en `~/.jcode/memory/projects/<hash>.json`. Es un sistema distinto del vault y no se solapan.

Ojo con un bug del binario (verificado en 0.75.3, `fd1ff012c`): **el CLI `jcode memory` no lee el almacén que escribe el TUI**. `stats`, `list`, `search` y `export` reportan 0 memorias aunque en disco haya decenas, e `import` responde `Imported 1 memories` sin persistir nada ni encontrarlo después en `search`. Los ficheros de `~/.jcode/memory/projects/` sí se actualizan durante la sesión, así que la memoria del TUI funciona; lo que está roto es la vista del CLI. Mientras siga así, para inspeccionar esas memorias hay que leer el JSON directamente:

```bash
python3 -c "
import json,pathlib
for f in sorted(pathlib.Path.home().glob('.jcode/memory/projects/*.json')):
    d=json.loads(f.read_text()); ms=d.get('memories',{})
    print(f'{f.name}: {len(ms)} memorias')
    for m in list(ms.values())[:3]:
        print('  -', m['content'][:100])
"
```

Verificación local:

```bash
python3 self_check_jcode.py
```

## Commits

Los mensajes de commit son la cuarta fuente, y la que más cobertura aporta. No solapan con claude-mem:

- **claude-mem** registra el razonamiento de la *sesión*: qué se investigó, qué se descartó, los callejones sin salida.
- **Los commits** registran lo *aceptado*: qué cambió de verdad y por qué, redactado a posteriori.

Y aquí el enlace es **exacto, no heurístico**: `git log --name-only` dice con certeza qué ficheros tocó cada commit, así que no interviene el matcher por sufijo.

Cubren sobre todo los puntos ciegos —ficheros que nadie ha trabajado con Claude pero que sí tienen historia:

| proyecto | con memoria | ciegos | ciegos **con commits** | cobertura |
|---|---|---|---|---|
| EVOTEKNIC | 189 | 374 | 360 (96 %) | 210 → **1.510 de 1.548** |
| MYC18 | 436 | 1.218 | 576 | 436 → 1.012 |
| REPO-TERM | 17 | 18 | 17 (94 %) | 17 → 34 |

Sobre los 21 proyectos reales: **cobertura global del 75 %** (14.672 notas con historia de 19.603).

Se filtran los commits sin contenido (`wip`, `typo`, `merge branch`, `bump`…) y los que no tocan ningún fichero del vault. En estos repos el filtro apenas trabaja: entre 0 y 10 triviales sobre cientos de commits. Tope de 1.500 por raíz. Con `--no-commits` se omite la categoría.

## Reconstrucción del índice de graft

`refresh_vaults.py` ejecuta `graft build` en cada raíz antes de generar, para que el vault no se construya sobre un grafo de código viejo. Se desactiva con `--no-build`, o por proyecto con `"no_build": true` en la configuración.

Solo refresca los índices **que ya existen**. Crear uno nuevo en una raíz es una decisión con consecuencias —un repo que trae Odoo o los addons de OCA dentro produce un índice inservible— y esa decisión la toma quien monta el proyecto, no el script. También se salta los índices ya desmesurados: reconstruirlos tarda medio minuto y luego se descartan igual.

Es barato porque graft cachea: 27 repos en 12 s, replayando lo no modificado.

**Fallback de stack-size.** Un repo con demasiados ficheros por indexar puede hacer que `graft build` reviente el stack de Node (`RangeError: Maximum call stack size exceeded`) en vez de terminar con un índice desmesurado. `graft_build()` detecta ese `stderr` concreto y reintenta una vez con `node --stack-size=65500 $(which graft) build` — el mismo workaround que el alias manual `graft-build-big` de `~/.zshrc`. Es automático: no hace falta detectarlo ni relanzar nada a mano. Si el fallo es de otro tipo, no reintenta; se propaga tal cual a `saltados`.

## Matching en tres capas

De más precisa a más permisiva; se acepta la primera que resuelve:

1. **Raíz + ruta relativa exacta** — cuando la ruta observada cae dentro de una raíz conocida. Es la que aporta HUB+SPOKE y la de mayor confianza.
2. **Sufijo de ruta más largo** que coincida con una ruta relativa completa del grafo, y solo si es inequívoco.
3. **Sufijo corto** (1-3 segmentos), también solo si es inequívoco. Red de seguridad para las rutas relativas sueltas que claude-mem guarda sin raíz.

Las tres exigen coincidencia inequívoca: el generador prefiere no enlazar a enlazar mal.

La prosa de las observaciones se escapa antes de escribirla, porque puede contener `[[…]]` —fragmentos de código, sobre todo— que Obsidian interpretaría como wikilinks fantasma.

## Guardas

- **Índices desmesurados.** Un `graft build` que atrapó `node_modules`, los addons de OCA o un árbol Docker completo aporta decenas de miles de ficheros ajenos. Por encima de `--max-files` la raíz se descarta y se avisa. Casos reales: BARSEL-MIGRATION indexó 27.592 ficheros (15.398 de `addons-v14`, 11.720 de `oca`) e IDRUS-MIG 77.085 (52.421 de `docker/`). La solución real es acotar el `graft build`, no subir el umbral.
- **`graft build` que revienta el stack.** Distinto del punto anterior: aquí el build ni siquiera termina, Node aborta con `RangeError: Maximum call stack size exceeded` por exceso de ficheros. Ver "Reconstrucción del índice de graft" — se reintenta solo con `--stack-size` ampliado, sin intervención manual.
- **Regeneración limpia.** Cada ejecución borra las notas de la anterior; si no, un vault que encoge deja huérfanas.
- **Destino ajeno.** El borrado solo actúa si `INDEX.md` lleva la firma del generador. Si apuntas a un directorio que no generamos nosotros, aborta sin tocar nada.

### Un proyecto repartido en varios nombres

claude-mem archiva por directorio de trabajo, así que trabajar desde los spokes de un proyecto genera nombres de proyecto distintos para lo que es el mismo trabajo. `custom-addons` es un caso real: 143 observaciones que son de MYC18 pero quedaron archivadas aparte, invisibles en su vault aunque los ficheros sí estuvieran indexados.

Se unen con `also` en la configuración:

```json
"MYC18": {
  "hub": "~/ai/HUB/MYC18",
  "also": ["custom-addons"]
}
```

O directamente, con comas: `graft_mem_vault.py <hub> MYC18,custom-addons <vault>`.

El primer nombre da nombre al vault; el resto solo aportan observaciones. En MYC18 esto subió el enganche de 1.211 a 1.298. `vault_lookup` también busca en todos los nombres, así que la búsqueda por contenido las encuentra igual.

Para detectar candidatos, busca proyectos de claude-mem cuya raíz git más referenciada caiga dentro de otro proyecto:

```bash
sqlite3 -readonly ~/.claude-mem/claude-mem.db \
  "SELECT project, count(*) FROM observations GROUP BY project ORDER BY 2 DESC;"
```

### Acotar un repo que trae Odoo o OCA dentro

`graft build` no admite exclusiones ni respeta `.gitignore`, así que un repo que lleve dentro un checkout de Odoo o los addons de OCA produce un índice inservible. La salida es construir graft **solo en los subdirectorios con código propio** y declararlos con `--root`:

```bash
# el índice del hub tiene 77.085 ficheros: 52.421 de docker/ (Odoo) y 12.720 de sources/
cd ~/ai/HUB/IDRUS-MIG/scripts && graft build
cd ~/ai/HUB/IDRUS-MIG/custom_addons/v16/idrus-addons && graft build

python3 graft_mem_vault.py ~/ai/HUB/IDRUS-MIG IDRUS-MIG ~/vaults/IDRUS-MIG-graft-mem \
    --root=~/ai/HUB/IDRUS-MIG/scripts \
    --root=~/ai/HUB/IDRUS-MIG/docs \
    --root=~/ai/HUB/IDRUS-MIG/custom_addons/v16/idrus-addons
```

El índice desmesurado del hub se descarta solo, y las raíces acotadas entran en su lugar. En IDRUS-MIG el enganche pasó de **3 a 525 observaciones**. Cuando varias raíces se solapan, la más específica gana: una ruta bajo `scripts/` se resuelve contra `scripts/`, no contra el hub.

Una raíz explícita **no** tiene que ser raíz git: los subdirectorios de un mismo repo valen, y de hecho son el caso de uso. Si generas la lista en un bucle de zsh, acuérdate de usar un array (`ROOTS+=(--root="$d")`): zsh no hace word-splitting de variables sin comillas y las raíces llegarían como un único argumento.

### `variants: N` en una migración

Cuando el repo guarda el mismo addon para varias versiones de Odoo (`custom_addons/v15/`, `v16/`, `v17/`…), las copias se fusionan por ruta relativa en una nota por fichero lógico, y el frontmatter registra cuántas versiones difieren. En IDRUS-MIG eso marca **470 ficheros que cambian entre versiones**: precisamente la superficie de riesgo de la migración.

## Limitaciones

- **El vault es generado, no editable.** Si escribes notas a mano dentro, la siguiente ejecución las borra. Mantén tus anotaciones en un vault aparte y enlaza.
- **La calidad depende del índice de graft.** Si no cubre el lenguaje del repo (p. ej. el Rust de `src-tauri/` en un proyecto Tauri), esos ficheros no existen para el vault.
- **El descubrimiento de spokes es estadístico.** Un repo ajeno muy tocado en una sesión puede entrar como raíz; uno poco tocado puede quedarse fuera. Ajusta con `--min-refs`.
- **Sin filtro temporal.** Se incluyen todas las observaciones del proyecto.
- **Hay techos estructurales al enganche.** Las observaciones que referencian rutas de un servidor remoto (`/home/odoo/apps`, `/var/log/odoo`), documentación o scripts de shell no pueden enlazar con nada: esos ficheros no están en local o graft no parsea su lenguaje. En IDRUS-MIG eso explica la mayor parte de las 2.868 observaciones que no enganchan — 652 apuntan a `/home/odoo/apps` y 122 a scripts `.sh`.

## Verificación

```bash
python3 self_check.py         # generador y consulta
python3 self_check_hooks.py   # contrato de los hooks y fail-open
python3 self_check_codex.py   # extractores Codex e instalación idempotente
python3 self_check_jcode.py   # puente de overlay e instalación jcode
python3 self_check_scout.py   # recorte y validación de referencias del scout
bash -n install.sh install-codex.sh
bash install-codex.sh --check # dependencias y descubrimiento de claude-mem, sin escribir
```

`self_check.py` cubre 16 casos sobre repos git y base de datos sintéticos: descubrimiento de spokes, exclusión de repos ajenos, fusión hub/spoke con aviso de checkouts divergidos, guarda de índice desmesurado, las tres capas de matching, notas de documento con contenido completo y código citado, commits reales con filtrado de triviales y de los que no tocan el vault, unión de varios nombres de proyecto en un vault (y su no-contaminación al pedir uno solo), desambiguación de nombres en sistemas de ficheros insensibles a mayúsculas, clasificación automática y resolución de proyecto de `vault_lookup` (incluida la de rutas con enlace simbólico), escapado de prosa, puntos ciegos, limpieza en regeneración, protección de destinos ajenos e integridad de todos los wikilinks.

`self_check_hooks.py` verifica que los tres hooks emiten JSON válido y salen con 0 ante dieciséis entradas malformadas o fuera de proyecto, porque un hook que revienta bloquea la sesión entera.

`self_check_codex.py` monta hogares temporales y no toca la configuración real. Comprueba el extractor de rutas de herramientas Codex, el contrato fail-open, dos instalaciones consecutivas sin duplicados, la conservación de un hook ajeno, la copia de seguridad, el descubrimiento de claude-mem cuando solo existe bajo `$CODEX_HOME/plugins/cache/` y la regresión que impide que el nudge de `PostToolUse` emita `updatedMCPToolOutput` o `additionalContext`.

`self_check_jcode.py` monta hogares temporales y no toca la configuración real. Comprueba la extracción de `additionalContext`, el orden del overlay, la escritura segura de `.jcode/prompt-overlay.md`, el merge idempotente de hooks de jcode y el fallback del MCP desde `~/.jcode/graft-mem-vault/scripts/` a `~/.claude/scripts/`.

Los checks automáticos no sustituyen la prueba del harness. Tras instalar, la verificación de integración es: reiniciar Codex, aprobar `/hooks`, abrir una sesión nueva, ejecutar `/clear`, realizar una lectura y un cambio local, cerrar el turno y confirmar que las observaciones aparecen en la base común. El contexto inmediato prueba claude-mem; `vault_lookup.py --stats` después del refresco prueba la repercusión en graft y Obsidian.

## Hallazgos del montaje

Cosas que no están documentadas en ningún sitio y que costaron encontrar. Se dejan aquí porque cualquiera que reproduzca esto tropezará con las mismas.

**graft no respeta `.gitignore`.** Indexa lo que encuentra en el árbol, incluido lo gitignorado. En IDRUS-MIG eso significaba 77.085 ficheros: 52.421 de un `docker/` con Odoo dentro y 12.720 de un `sources/` gitignorado. Tampoco admite exclusiones (`graft build` solo tiene `--extensions` y un directorio), así que la única vía para acotar es construir en subdirectorios y declararlos con `--root`.

**claude-mem guarda las rutas de forma heterogénea.** Mezcla absolutas, con `~` y relativas al repo, y la raíz que registra no coincide con la raíz donde se construyó graft. De ahí el matcher de tres capas.

**El vínculo hub→spoke no está declarado en ninguna parte.** No hay symlinks, ni manifiesto, ni nada en `.mcp.json` o `CLAUDE.md`. Se deduce de la propia claude-mem remontando cada ruta observada hasta su raíz git.

**Obsidian reescribe su registro de vaults al cerrarse.** Dar de alta un vault con la aplicación abierta se pierde en silencio. Y el URI `obsidian://open?path=…` solo funciona con vaults ya registrados, así que no sirve para el alta inicial. En macOS, además, cerrar la ventana no cierra la aplicación: hace falta ⌘Q.

**En macOS `/var` es un enlace a `/private/var`,** y `~/.claude` suele serlo a otro directorio. Comparar rutas sin `realpath` hace que la resolución del proyecto falle sin motivo aparente.

**`sys.exit("mensaje")` no escribe en stdout** cuando se captura la excepción, así que un fallo dentro de un test se ve como salida vacía e indiagnosticable. Los checks recogen `e.code` explícitamente.

**En zsh las variables sin comillas no hacen word-splitting** (a diferencia de bash), así que generar una lista de `--root` en un bucle y pasarla sin array manda todas las raíces como un único argumento.

**`cp` suele estar aliaseado a `cp -i`**, que en un script no interactivo se queda esperando confirmación para siempre. Y `ls` puede estarlo a `eza`, que no muestra ocultos igual: al depurar nombres de fichero conviene usar `os.listdir` en vez de fiarse de la terminal.

**macOS (APFS) y Windows no distinguen mayúsculas en los nombres de fichero.** Dos rutas trackeadas como `.github/PULL_REQUEST_TEMPLATE.md` y `.github/pull_request_template.md` —una por raíz, cosa que pasa cuando dos repos del mismo proyecto las escriben distinto— generan nombres de nota distintos pero un único fichero: la segunda escritura pisa a la primera y el enlace de la perdedora queda roto. Los nombres se desambiguan con un sufijo derivado de la ruta, asignado por orden lexicográfico para que no dependa del orden de recorrido.

**`%x00` en el `--format` de git no se puede pasar como carácter literal en `argv`**: hay que dejar que git lo expanda (`--format=%x01%H%x1f…`) en vez de interpolarlo en Python.

**Codex no es Claude Code con otro nombre de directorio.** Sus hooks viven en `~/.codex/hooks.json`, ejecutan grupos coincidentes en paralelo y exigen confianza por hash. La adaptación importa los handlers nativos de claude-mem, amplía `SessionStart` con `clear` y mantiene el refresco costoso desprendido; copiar únicamente los tres scripts del vault no registraría la memoria de la sesión de Codex.

**La salida admitida por `PostToolUse` depende también de la normalización del harness.** La [documentación oficial de hooks de Codex](https://learn.chatgpt.com/docs/hooks.md) admite `systemMessage` y contexto adicional, pero en la ruta MCP observada `additionalContext` acabó convertido en `updatedMCPToolOutput`, que Codex rechaza. `systemMessage` aporta el mismo nudge sin intentar mutar el resultado de la herramienta.

## Referencia de comandos

```bash
# generar y refrescar
refresh_vaults.py                      # todos los proyectos + alta en Obsidian
refresh_vaults.py --only PROY          # uno
refresh_vaults.py --register-only      # solo el alta en Obsidian
refresh_vaults.py --dry-run            # enseña los comandos sin ejecutarlos
refresh_vaults.py --no-register        # no toca Obsidian
refresh_vaults.py --no-build           # no refresca los índices de graft

# instalar o actualizar la integración global de Codex
bash install-codex.sh --check          # comprueba sin escribir
bash install-codex.sh                  # fusiona hooks y copia scripts
python3 self_check_codex.py            # regresiones portables, sin tocar ~/.codex

# alta inicial de un proyecto nuevo: projects.json + primer índice explícito
cd ~/ai/HUB/PROYECTO && graft build
python3 ~/.codex/graft-mem-vault/scripts/refresh_vaults.py --only PROYECTO
python3 ~/.codex/graft-mem-vault/scripts/vault_lookup.py --stats

# generar uno suelto, sin pasar por la configuración
graft_mem_vault.py <hub> <proyecto> <dir-vault> [--root=…] [--min-refs=N]
                   [--max-files=N] [--no-docs] [--no-commits]
                   [--no-sessions] [--build]

# consultar (el tipo de consulta se deduce solo)
vault_lookup.py models/sale_order.py   # ficha del fichero
vault_lookup.py 29052                  # abre esa observación
vault_lookup.py gastos_devolucion      # observaciones + sesiones + documentos
vault_lookup.py --ses <id>             # la sesión: qué se pidió, qué se aprendió
vault_lookup.py --stats                # qué hay en el vault de este repo
vault_lookup.py --global <termino>     # busca en TODOS los vaults, no solo el de este repo
vault_lookup.py --global --obs proy:id # abre una observacion de otro proyecto
# la ficha de un fichero incluye sus commits además de sus observaciones

# verificar
python3 self_check.py                  # generador y consulta
python3 self_check_hooks.py            # contrato de los hooks y fail-open
python3 self_check_codex.py            # instalación e integración de Codex
python3 self_check_jcode.py            # overlay e integración de jcode
python3 self_check_scout.py            # scout y referencias
bash -n install.sh install-codex.sh    # sintaxis de instaladores
bash install.sh --check                # dependencias
```

## Relación con otros tricks

| Trick | Relación |
|-------|----------|
| **memory-engineering** | Limpia el almacén de claude-mem antes de generar; menos duplicados, mejor vault |
| **fullmemory** | Es quien alimenta `~/.claude-mem/claude-mem.db` con observaciones |
| **graphify** | Ruta alternativa: construye su propio grafo y exporta a Obsidian, pero ignora `wiring.json` y la memoria |
| **tldr-over-grep** | Mismo principio: entender el código sin leerlo entero |
