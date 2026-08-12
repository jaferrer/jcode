#!/usr/bin/env python3
"""
Servidor MCP (stdio, sin dependencias) que expone el vault del proyecto.

Envuelve vault_lookup.py y vault_scout.py como tools, para que Claude Code,
Codex y jcode accedan al vault por la misma vía. El transporte es JSON-RPC 2.0
línea a línea por stdin/stdout, con protocolo 2024-11-05.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROTOCOL = "2024-11-05"
TIMEOUT = 60
SERVER_NAME = "vault"
SERVER_VERSION = "1.0.0"


TOOLS = [
    {
        "name": "vault_lookup",
        "description": (
            "Consulta el vault del proyecto por fichero, término, id de "
            "observación o de sesión. Devuelve el índice o la entrada concreta."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "termino": {
                    "type": "string",
                    "description": "Fichero, término o id a consultar.",
                }
            },
            "required": ["termino"],
        },
    },
    {
        "name": "vault_scout",
        "description": (
            "Busca en el vault por concepto y devuelve hechos recortados con "
            "su id, en vez de volcar observaciones enteras."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pregunta": {
                    "type": "string",
                    "description": "Pregunta en lenguaje natural.",
                }
            },
            "required": ["pregunta"],
        },
    },
]


def scripts_dir():
    """Directorio de scripts, compatible con instalaciones antiguas y jcode.

    Orden de resolución, de más explícito a más genérico:

      1. GRAFT_MEM_VAULT_SCRIPTS  — override directo
      2. $GRAFT_MEM_VAULT_HOME/scripts — lo exporta install-codex.sh
      3. ~/.jcode/graft-mem-vault/scripts
      4. ~/.codex/graft-mem-vault/scripts
      5. ~/.claude/scripts — instalaciones antiguas

    Sin los pasos 2 y 4 un host solo-Codex caía siempre en ~/.claude/scripts
    y el servidor respondía "No encontrado"; el fallo quedaba enmascarado en
    máquinas que además tuvieran Claude Code instalado.
    """
    explicit = os.environ.get("GRAFT_MEM_VAULT_SCRIPTS")
    if explicit:
        return Path(explicit).expanduser()

    package = os.environ.get("GRAFT_MEM_VAULT_HOME")
    if package:
        package_scripts = Path(package).expanduser() / "scripts"
        if package_scripts.exists():
            return package_scripts

    for harness in ("~/.jcode", "~/.codex"):
        candidate = Path(harness).expanduser() / "graft-mem-vault" / "scripts"
        if candidate.exists():
            return candidate

    return Path("~/.claude/scripts").expanduser()


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def error_reply(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def text_content(text):
    return {"content": [{"type": "text", "text": text}]}


def run_script(script_name, argument):
    script = scripts_dir() / script_name
    if not script.exists():
        return f"No encontrado: {script}"

    try:
        proc = subprocess.run(
            [sys.executable, str(script), argument],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Timeout ejecutando {script_name} tras {TIMEOUT}s"
    except Exception as error:  # fail-open para el cliente MCP
        return f"Fallo al ejecutar {script_name}: {error}"

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        suffix = f"\n(exit {proc.returncode})"
        return (output.strip() + suffix).strip()
    return output or "(sin resultados)"


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "vault_lookup":
        return text_content(run_script("vault_lookup.py", str(arguments.get("termino", ""))))
    if name == "vault_scout":
        return text_content(run_script("vault_scout.py", str(arguments.get("pregunta", ""))))
    return text_content(f"Tool desconocida: {name}")


def handle_request(request):
    request_id = request.get("id")
    method = request.get("method")

    # Las notificaciones no llevan id y no se responden.
    if request_id is None:
        return

    if method == "initialize":
        reply(
            request_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    elif method == "tools/list":
        reply(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = request.get("params") or {}
        reply(request_id, call_tool(params.get("name", ""), params.get("arguments") or {}))
    else:
        error_reply(request_id, -32601, f"Método no soportado: {method}")


def main():
    for line in sys.stdin:
        request = None
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # Entrada MCP inválida: fail-open sin reutilizar ids previos ni contaminar stdout.
            continue
        except Exception:
            # Fail-open defensivo ante entradas inesperadas sin traceback ni stdout.
            continue

        try:
            if isinstance(request, dict):
                handle_request(request)
        except Exception as error:
            # No emitir tracebacks en stdout. Si hay id recuperable, responder error JSON-RPC.
            request_id = request.get("id") if isinstance(request, dict) else None
            if request_id is not None:
                error_reply(request_id, -32603, f"Error interno: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
