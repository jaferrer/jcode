# Vault del proyecto: consulta antes de investigar

Muchos repos tienen un **vault** que cruza el grafo de código de graft con la memoria de claude-mem y los documentos del proyecto. Contiene el historial de incidencias, decisiones y hallazgos por fichero.

**Antes de investigar un fichero, consulta el vault.** Un `grep` te dice qué hay en el código hoy; el vault te dice qué pasó con él y por qué está así.

```bash
python3 ~/.claude/scripts/vault_lookup.py <fichero-o-termino>   # índice: ~450 tokens
python3 ~/.claude/scripts/vault_lookup.py --obs <id> [id ...]   # abre observaciones concretas
python3 ~/.claude/scripts/vault_lookup.py --ses <id>            # abre una sesión entera
python3 ~/.claude/scripts/vault_lookup.py --doc <ruta>          # abre un documento o config
python3 ~/.claude/scripts/vault_lookup.py --stats               # qué hay en el vault de este repo
```

## La sesión responde al «por qué»

Una búsqueda por concepto devuelve tres capas: observaciones (qué se tocó), **sesiones** (qué se pretendía) y documentos. La sesión trae lo que se pidió, lo que se investigó, lo que se aprendió y lo que quedó abierto, agrupando toda una tanda de trabajo.

Cuando la pregunta sea «¿ya intentamos esto?» o «¿por qué está montado así?», la sesión es la entrada correcta; para «¿qué le pasó a este fichero?», la observación. Los documentos incluyen la configuración del proyecto (`*.yaml`, `*.toml`, `.claude/*.json`), que es donde suele estar escrita la decisión.

## Por defecto es por repo — para cruzar todos los proyectos, `--global`

El vault resuelto es siempre el del repo actual. Cuando la pregunta no es de este
repo — arquitectura de una herramienta que se usa en varios proyectos, "¿dónde
resolvimos ya esto?" sin fichero concreto — usa `--global` para buscar en todos
los vaults a la vez, etiquetado por proyecto:

```bash
python3 ~/.claude/scripts/vault_lookup.py --global <termino>
python3 ~/.claude/scripts/vault_lookup.py --global --obs <proyecto>:<id>   # abrir una concreta
```

Dos formas de disparar `--global`, ambas válidas: el usuario lo pide explícitamente, o **tú** te
das cuenta de que la pregunta excede el scope del repo actual y lo lanzas por iniciativa
propia. El hook de `UserPromptSubmit` ayuda a lo segundo: si no hay nada en el vault de
este repo pero sí lo hay en otros, avisa de eso en vez de callar — es la señal para
probar `--global` en lugar de investigar desde cero.

## El disparo suele ser automático

Tres hooks inyectan el histórico sin que nadie lo pida: al arrancar la sesión (incluido `/clear`), al enviar un prompt cuya consulta tiene material en el vault, y al ir a leer un fichero con historia. **Cuando veas un bloque `[vault]`, léelo antes de decidir cómo investigar** — suele ahorrar la investigación entera.

Los hooks son una red, no una garantía: no cubren un `Grep` o un `graft ask` que no venga precedido de una pregunta. Ahí la consulta la lanzas tú.

## Cuándo

- **Antes de tocar un fichero** que no conoces: `vault_lookup.py ese_fichero.py`
- **Ante un bug que suena a conocido**: busca por el nombre del fichero implicado antes de depurar desde cero
- **Al arrancar en un módulo nuevo**: `--stats` y luego los ficheros con más historia
- **Al buscar el porqué de algo raro**: el vault suele tener la observación que lo explica

## Cómo, sin quemar contexto

Dos pasos, siempre:

1. **El índice primero.** Devuelve símbolos, número de observaciones, documentos que lo citan y los títulos de las 15 observaciones más recientes. Cuesta unos cientos de tokens.
2. **Solo entonces, la observación concreta** con `--obs <id>`, guiándote por los títulos. O la sesión con `--ses <id>` si lo que falta es el contexto de por qué se hizo aquello.

Volcar todo el historial de un fichero muy trabajado son miles de tokens y casi nunca hace falta. Si el índice no basta, `--all` lista todos los títulos antes de abrir nada.

## Qué esperar

- Si el directorio no pertenece a ningún proyecto con vault, el comando lo dice y no pasa nada: sigue con tu método normal.
- «sin memoria asociada» significa que nadie ha trabajado ese fichero con Claude. Es información: estás en territorio virgen.
- El vault es una foto del último `refresh_vaults.py`, no tiempo real. Para lo de esta misma sesión, tu contexto manda.

## No confundir

- **graft** responde «dónde está y quién lo llama» sobre el código de ahora.
- **vault_lookup** responde «qué nos pasó con esto» sobre el histórico.

Son complementarios: graft para navegar, vault para no repetir investigaciones ya hechas.
