#!/usr/bin/env python3
"""SessionStart: inyecta el estado del vault y lanza su refresco en segundo plano.

Se dispara tambien en /clear y /compact, que es cuando el contexto se ha vaciado
y mas falta hace saber que hay memoria disponible.

El refresco no puede ir en primer plano: IDRUS-MIG tarda 24 s y el hook agotaria
su timeout. Se lanza desprendido y el contexto que se inyecta es el del vault
actual; la sesion siguiente vera el refrescado. Un vault de hace unos minutos es
util igual, porque su valor es el historico, no lo de hace treinta segundos.

Fail-open por construccion: cualquier error emite {} y la sesion sigue.
"""
import json
import os
import re
import subprocess
import sys

CONFIG_ROOT = os.path.expanduser(
    os.environ.get("GRAFT_MEM_VAULT_HOME")
    or os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
SCRIPTS = os.path.join(CONFIG_ROOT, "scripts")
LOOKUP = os.path.join(SCRIPTS, "vault_lookup.py")
LOCK = "/tmp/graft-mem-vault-refresh.lock"
LOCK_TTL = 300  # s: si el lock es mas viejo, el refresco anterior murio


def emitir(obj=None):
    sys.stdout.write(json.dumps(obj) if obj else "{}")
    sys.exit(0)


def refresco_en_curso():
    """Evita apilar refrescos si se hacen varios /clear seguidos."""
    try:
        edad = os.path.getmtime(LOCK)
    except OSError:
        return False
    import time
    if time.time() - edad > LOCK_TTL:
        try:
            os.remove(LOCK)
        except OSError:
            pass
        return False
    return True


def lanzar_refresco(proyecto):
    if refresco_en_curso():
        return False
    try:
        open(LOCK, "w").write(proyecto)
        subprocess.Popen(
            ["/bin/sh", "-c",
             f"python3 {SCRIPTS}/refresh_vaults.py --only {proyecto} --no-register"
             f" >/dev/null 2>&1; rm -f {LOCK}"],
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        try:
            os.remove(LOCK)
        except OSError:
            pass
        return False


def resumen(vault, proyecto, refrescando):
    idx = os.path.join(vault, "INDEX.md")
    try:
        texto = open(idx, errors="replace").read()
    except OSError:
        return ""
    cifras = [l[2:] for l in texto.splitlines() if l.startswith("- ") and "**" in l]
    top = []
    m = re.search(r"^## Codigo con mas historia\n\n(.*?)(?=\n## |\Z)", texto, re.M | re.S)
    if m:
        for linea in m.group(1).strip().splitlines()[:5]:
            t = re.match(r"\d+\. \[\[(.+?)\]\] — (\d+) observaciones", linea.strip())
            if t:
                top.append(f"  - {t.group(1)} ({t.group(2)} obs)")
    partes = [
        f"Este repo tiene vault de proyecto ({proyecto}): historico de incidencias,",
        "decisiones, sesiones y hallazgos cruzado con el grafo de codigo, los",
        "documentos y la configuracion del proyecto.",
        "",
    ]
    partes += [f"- {c}" for c in cifras]
    if top:
        partes += ["", "Ficheros con mas historia acumulada:"] + top
    partes += [
        "",
        "Consultalo ANTES de investigar un fichero, con dos pasos:",
        f"  python3 {LOOKUP} <fichero>   # indice, ~450 tokens",
        f"  python3 {LOOKUP} --obs <id>  # solo lo que interese",
        "Tambien: --ses <id> para una sesion completa (que se pidio, que se",
        "aprendio), --doc <ruta> para un documento, --stats para el resumen.",
    ]
    if refrescando:
        partes += ["", "(refrescandose en segundo plano; estas cifras son de la pasada anterior)"]
    return "\n".join(partes)


def main():
    try:
        sys.stdin.read()  # el payload no hace falta: el cwd basta
    except Exception:
        pass
    sys.path.insert(0, SCRIPTS)
    try:
        import vault_lookup as L
        proyecto, vault = L.resolver_vault()
    except SystemExit:
        emitir()  # el repo no tiene vault: no molestamos
    except Exception:
        emitir()

    refrescando = lanzar_refresco(proyecto)
    ctx = resumen(vault, proyecto, refrescando)
    if not ctx:
        emitir()
    emitir({
        "continue": True,
        "result": "continue",
        "systemMessage": ctx,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emitir()
