#!/usr/bin/env python3
"""Autocomprobacion de los hooks: contrato de salida y fail-open.

Un hook que rompe bloquea la sesion entera, asi que lo que se verifica sobre todo
es que ante cualquier entrada rara emitan JSON valido y salgan con codigo 0.

Uso: python3 self_check_hooks.py
"""
import json
import os
import subprocess
import sys
import tempfile

AQUI = os.path.dirname(os.path.abspath(__file__))
NUDGE = os.path.join(AQUI, "hooks", "vault-read-nudge.py")
START = os.path.join(AQUI, "hooks", "vault-session-start.py")
PROMPT = os.path.join(AQUI, "hooks", "vault-prompt-search.py")


def lanzar(script, entrada, cwd=None, env=None):
    r = subprocess.run([sys.executable, script], input=entrada, capture_output=True,
                       text=True, cwd=cwd, env=env, timeout=30)
    return r


def main():
    fallos = []
    with tempfile.TemporaryDirectory() as tmp:
        # Entradas que un hook mal escrito no aguanta. Todas deben dar JSON y exit 0.
        casos = [
            ("vacia", ""),
            ("no json", "esto no es json"),
            ("json vacio", "{}"),
            ("sin tool_input", '{"tool_name":"Read"}'),
            ("file_path nulo", '{"tool_name":"Read","tool_input":{"file_path":null}}'),
            ("otra tool", '{"tool_name":"Bash","tool_input":{"command":"ls"}}'),
            ("fuera de proyecto",
             '{"tool_name":"Read","tool_input":{"file_path":"/tmp/nada/x.py"}}'),
            ("ruta inexistente",
             '{"tool_name":"Read","tool_input":{"file_path":"/no/existe/y.py"}}'),
            ("extension ajena",
             '{"tool_name":"Read","tool_input":{"file_path":"/tmp/a.bin"}}'),
        ]
        for nombre, entrada in casos:
            r = lanzar(NUDGE, entrada, cwd=tmp)
            if r.returncode != 0:
                fallos.append(f"read-nudge sale {r.returncode} con entrada {nombre!r}")
                continue
            try:
                json.loads(r.stdout or "{}")
            except ValueError:
                fallos.append(f"read-nudge emite JSON invalido con entrada {nombre!r}:"
                              f" {r.stdout[:80]!r}")

        # El hook de sesion, en un directorio sin vault, no debe inyectar nada
        # ni lanzar refrescos: el usuario puede estar en cualquier sitio.
        r = lanzar(START, "{}", cwd=tmp)
        if r.returncode != 0:
            fallos.append(f"session-start sale {r.returncode} fuera de un proyecto")
        else:
            try:
                d = json.loads(r.stdout or "{}")
                if d.get("hookSpecificOutput"):
                    fallos.append("session-start inyecta contexto fuera de un proyecto")
            except ValueError:
                fallos.append(f"session-start emite JSON invalido: {r.stdout[:80]!r}")

        # El hook de prompt: fuera de un proyecto, y ante prompts que no son
        # consultas, no debe inyectar nada. Y aguanta cualquier basura de entrada.
        for nombre, entrada in [
            ("vacia", ""), ("no json", "{{{"), ("json vacio", "{}"),
            ("prompt nulo", '{"prompt":null}'),
            ("prompt corto", '{"prompt":"si"}'),
            ("prompt sin terminos", '{"prompt":"vale gracias sigue asi"}'),
            ("prompt normal", '{"prompt":"por que falla la conciliacion de efectos"}'),
        ]:
            r = lanzar(PROMPT, entrada, cwd=tmp)
            if r.returncode != 0:
                fallos.append(f"prompt-search sale {r.returncode} con entrada {nombre!r}")
                continue
            try:
                d = json.loads(r.stdout or "{}")
            except ValueError:
                fallos.append(f"prompt-search emite JSON invalido con {nombre!r}")
                continue
            if d.get("hookSpecificOutput"):
                fallos.append(f"prompt-search inyecta contexto fuera de un proyecto ({nombre})")

        # Y sin stdin en absoluto (algunos arranques no envian payload).
        r = lanzar(START, "", cwd=tmp)
        if r.returncode != 0:
            fallos.append("session-start no aguanta stdin vacio")

    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("OK — 3 hooks: contrato de salida y fail-open ante entradas rotas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
