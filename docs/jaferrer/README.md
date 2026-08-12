# Adaptaciones de jaferrer para jcode

Este fork es el hogar canónico de las adaptaciones locales de jcode. El código
upstream permanece sincronizable mediante el remote `upstream`; las extensiones
propias se desarrollan en ramas `feat/jaferrer-*` y se integran en el fork tras
validarlas.

## Contenido trasladado

- `integrations/graft-mem-vault/`: integración completa entre jcode,
  claude-mem, graft y el vault del proyecto.
- `scripts/jaferrer/omniroute-jcode-sync.*`: sincronización del catálogo de
  modelos OmniRoute con jcode.
- `docs/jaferrer/`: decisiones, plan, especificación y handoffs históricos.
- Rama `feat/graft-savings-widget`: widget TUI que acumula los footers
  `[graft] tokens saved ≈ N` devueltos por herramientas MCP.

## Remotes

```text
origin    git@github.com:jaferrer/jcode.git
upstream  https://github.com/1jehuang/jcode.git
```

Sincronización habitual:

```bash
git fetch upstream
git checkout master
git merge --ff-only upstream/master
git push origin master
```

## graft-mem-vault

Instalación o actualización:

```bash
bash integrations/graft-mem-vault/install-jcode.sh
python3 integrations/graft-mem-vault/self_check_jcode.py
python3 integrations/graft-mem-vault/self_check_hooks.py
```

El instalador copia los scripts y hooks a `~/.jcode/graft-mem-vault/`, instala
el gate `pre_tool` y cablea `session_start`, `session_end`, `post_tool` y
`turn_end` en `~/.jcode/config.toml` sin sobrescribir claves existentes.

El overlay del primer prompt puede sufrir una carrera si se depende únicamente
de `session_start`. Se recomienda conservar un wrapper de shell que ejecute:

```bash
~/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py --context
```

antes de iniciar el binario.

## Memoria y vault

El proyecto canónico se registra como `jcode` con hub
`~/ai/HUB/jcode` en `~/.config/graft-mem-vault/projects.json`.

- Las sesiones nuevas arrancadas desde este directorio se atribuyen a `jcode`.
- Las observaciones históricas específicamente relacionadas con jcode pueden
  reasignarse de `guia` a `jcode` mediante una migración SQL respaldada.
- No se usa `also: ["guia"]`, porque eso mezclaría toda la memoria documental
  de la guía con la memoria del desarrollo de jcode.

Refresco manual:

```bash
python3 ~/.jcode/graft-mem-vault/scripts/refresh_vaults.py --only jcode
```

## Límites de la migración

La guía puede mantener una ficha documental que explique qué es jcode, pero el
código operativo, los parches del fork y la continuidad de desarrollo deben
vivir aquí. Los artefactos de instalación genéricos solo se conservarán en la
guía si siguen siendo parte de su catálogo reutilizable, no como fuente
canónica del fork.
