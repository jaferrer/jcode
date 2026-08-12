#!/usr/bin/env python3
"""PreToolUse (Read): avisa si el fichero que se va a leer tiene historico en el vault.

Es lo que hace que la consulta la instigue el modelo sin que nadie se acuerde de
pedirla: al ir a leer un fichero, si el vault sabe algo de el, se inyectan el
recuento y los tres titulos mas recientes. Con eso el modelo decide si ampliar.

Nunca bloquea la lectura y no repite el aviso del mismo fichero dentro del TTL:
un aviso util una vez es ruido a la tercera.

Fail-open por construccion: cualquier error emite {} y la lectura sigue.
"""
import hashlib
import json
import os
import re
import sys
import time

CONFIG_ROOT = os.path.expanduser(
    os.environ.get("GRAFT_MEM_VAULT_HOME")
    or os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
SCRIPTS = os.path.join(CONFIG_ROOT, "scripts")
LOOKUP = os.path.join(SCRIPTS, "vault_lookup.py")
TTL = 1800  # s sin repetir aviso del mismo fichero
TOPE = 3    # titulos que se inyectan; para mas, el modelo llama a vault_lookup


def emitir(obj=None):
    sys.stdout.write(json.dumps(obj) if obj else "{}")
    sys.exit(0)


def ya_avisado(ruta):
    marca = os.path.join(
        "/tmp", "vault-nudge-" + hashlib.sha1(ruta.encode()).hexdigest()[:16])
    try:
        if time.time() - os.path.getmtime(marca) < TTL:
            return True
    except OSError:
        pass
    try:
        open(marca, "w").close()
    except OSError:
        pass
    return False


def _ruta_en_objeto(obj):
    """Busca una ruta en la respuesta estructurada de un MCP."""
    if isinstance(obj, dict):
        for clave in ("file_path", "filePath", "path"):
            valor = obj.get(clave)
            if isinstance(valor, str) and valor:
                return valor
        for valor in obj.values():
            ruta = _ruta_en_objeto(valor)
            if ruta:
                return ruta
    elif isinstance(obj, list):
        for valor in obj:
            ruta = _ruta_en_objeto(valor)
            if ruta:
                return ruta
    elif isinstance(obj, str) and obj[:1] in "[{":
        try:
            return _ruta_en_objeto(json.loads(obj))
        except ValueError:
            pass
    return None


def _ruta_de_parche(texto):
    if not isinstance(texto, str):
        return None
    patrones = (
        r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
        r"^\+\+\+ (?:[ab]/)?(.+)$",
        r"^--- (?:[ab]/)?(.+)$",
    )
    for patron in patrones:
        encontrado = re.search(patron, texto, re.MULTILINE)
        if encontrado and encontrado.group(1).strip() != "/dev/null":
            return encontrado.group(1).strip()
    return None


def extraer_ruta(payload):
    """Obtiene el fichero afectado por Claude o por un hook de Codex."""
    herramienta = payload.get("tool_name")
    entrada = payload.get("tool_input") or {}
    if herramienta in ("Read", "Edit", "Write"):
        ruta = _ruta_en_objeto(entrada)
        return ruta or _ruta_de_parche(entrada.get("command", "") if isinstance(entrada, dict) else entrada)
    if herramienta == "apply_patch":
        if isinstance(entrada, dict):
            for clave in ("patch", "command"):
                ruta = _ruta_de_parche(entrada.get(clave, ""))
                if ruta:
                    return ruta
        return _ruta_de_parche(entrada)
    if herramienta == "mcp__codebase_memory_mcp__get_code_snippet":
        return _ruta_en_objeto(payload.get("tool_response") or payload.get("tool_output"))
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        emitir()
    ruta = extraer_ruta(payload) or ""
    if not ruta or not ruta.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".md")):
        emitir()

    sys.path.insert(0, SCRIPTS)
    try:
        import vault_lookup as L
        _, vault = L.resolver_vault(os.path.dirname(ruta) or None)
    except SystemExit:
        emitir()  # fuera de todo proyecto con vault
    except Exception:
        emitir()
    # de mas contexto a menos: un basename suelto suele repetirse entre modulos,
    # asi que se prueba primero con los ultimos segmentos de la ruta
    seg = ruta.strip("/").split("/")
    hits = []
    for n in range(min(4, len(seg)), 0, -1):
        hits = L.buscar(vault, "/".join(seg[-n:]))
        if len(hits) == 1:
            break
    if len(hits) != 1:
        emitir()  # ambiguo o sin coincidencia: que lo resuelva el modelo si quiere

    try:
        texto = open(hits[0][1], errors="replace").read()
    except OSError:
        emitir()
    entradas = [m for m in (re.match(r"- \[\[obs-(\d+)\]\] — (.*)", l.strip())
                            for l in L.seccion(texto, "Memoria").splitlines()) if m]
    if not entradas or ya_avisado(ruta):
        emitir()

    lineas = [f"El vault del proyecto tiene {len(entradas)} observaciones sobre "
              f"{os.path.basename(ruta)}. Las mas recientes:"]
    lineas += [f"  {m.group(1)}  {m.group(2)[:90]}" for m in entradas[:TOPE]]
    lineas.append(f"Amplia con: python3 {LOOKUP} "
                  f"{os.path.basename(ruta)}   (o --obs <id> para una concreta)")
    mensaje = "\n".join(lineas)
    salida = {"systemMessage": mensaje}
    evento = payload.get("hook_event_name", "PreToolUse")
    if evento != "PostToolUse":
        salida["hookSpecificOutput"] = {
            "hookEventName": evento,
            "additionalContext": mensaje,
        }
    emitir(salida)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emitir()
