#!/usr/bin/env python3
"""
Puente graft-mem-vault/claude-mem → jcode.

jcode no puede inyectar contexto desde un hook: los suyos son observadores
fire-and-forget, salvo pre_tool, que solo aprueba o deniega. Pero jcode sí
lee .jcode/prompt-overlay.md al ensamblar el prompt (paso 5 de su montaje).
Este puente recoge el additionalContext que ya producen claude-mem y
vault-session-start.py, y lo deposita en ese fichero.

Modos:
  --context      recupera y escribe el overlay
  --summarize    passthrough de escritura a claude-mem
  --observation  passthrough de escritura a claude-mem
  --track        registra el fichero tocado (se llama desde pre_tool)
  --turn-end     refresca el overlay si esta rancio (se llama desde turn_end)

Rastreo de ficheros
-------------------
claude-mem deduce files_read/files_modified de `tool_input.file_path`, y el
vault solo indexa observaciones que traigan alguno de los dos (el filtro de
`fetch_rows`). Pero el post_tool de jcode no expone el input de la
herramienta: solo tool_name, status, duration y output_bytes, con stdin
vacío. Sin rastreo, toda observación de jcode nace sin ficheros y el vault
la descarta.

El file_path sí está disponible en pre_tool, así que ahí se registra en un
spool por sesión que la siguiente observación vacía y adjunta.

Nunca falla ruidosamente: siempre sale con 0.
"""

import glob
import json
import os
import subprocess
import sys
import time

TIMEOUT = 5
SUMMARIZE_TIMEOUT = 60

VAULT_HOME = os.environ.get(
    "GRAFT_MEM_VAULT_HOME", os.path.expanduser("~/.jcode/graft-mem-vault")
)

# Herramientas que modifican frente a las que solo leen.
WRITE_TOOLS = {
    "edit", "write", "multiedit", "notebookedit", "apply_patch", "patch",
    "fast_edit", "fast_batch_edit", "fast_multi_edit",
}
READ_TOOLS = {"read", "notebookread"}


def state_dir():
    """Directorio del spool de rastreo (inyectable para los self-checks)."""
    base = os.environ.get("GRAFT_MEM_VAULT_STATE")
    if not base:
        base = os.path.expanduser("~/.jcode/state")
    return base


def _spool_path(session_id):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in
                   (session_id or "jcode"))[:120]
    return os.path.join(state_dir(), f"vault-files-{safe}.jsonl")


def track_tool_file(session_id, tool_name, tool_input):
    """Registra el fichero tocado por una herramienta. Nunca lanza."""
    try:
        if not isinstance(tool_input, dict):
            return False
        tool = (tool_name or "").strip().lower()
        path = (
            tool_input.get("file_path")
            or tool_input.get("filePath")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
        )
        if not path or not isinstance(path, str):
            return False
        if tool in WRITE_TOOLS:
            kind = "modified"
        elif tool in READ_TOOLS:
            kind = "read"
        else:
            return False
        os.makedirs(state_dir(), exist_ok=True)
        line = json.dumps({"kind": kind, "path": path})
        with open(_spool_path(session_id), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False


def drain_tracked_files(session_id):
    """Devuelve (leídos, modificados) y vacía el spool. Nunca lanza."""
    leidos, tocados = [], []
    try:
        path = _spool_path(session_id)
        if not os.path.exists(path):
            return ([], [])
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        try:
            os.remove(path)
        except OSError:
            pass
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            p, kind = rec.get("path"), rec.get("kind")
            if not p:
                continue
            if kind == "modified":
                if p not in tocados:
                    tocados.append(p)
            elif p not in leidos:
                leidos.append(p)
        # Un fichero modificado no se contabiliza además como leído.
        leidos = [p for p in leidos if p not in tocados]
    except Exception:
        return (leidos, tocados)
    return (leidos, tocados)


# Palabras que no discriminan: buscar por ellas devuelve medio vault.
VACIAS_INTENCION = set("""
a al algo ahora antes aqui asi aunque bien cada como con contra cual cuando de del desde
donde dos el ella ellos en entre era eres es esa ese eso esta este esto estoy fue ha hace
hacer hacia hasta hay la las le lo los mas me mi mucho muy no nos o para pero poco por
porque que quien se ser si sin sobre solo son su sus tambien te tiene todo tu un una uno
ver y ya ademas ahi cuanto estan hacen hizo hago mismo pasa paso puede queda quiero sale
sea segun sido tal tras vale varias varios
about after all also and any are as at be been but by can could do does for from get has
have how in into is it its just like make may more most no not of on one only or other
our out over should so some than that the their then there these they this those to up
use very was we were what when where which who why will with would you your
archivo fichero ficheros codigo file files code donde esta cual como que
hola adios gracias buenas vale okay bien perfecto claro venga sigue continua
dime haz pon mira oye ahora luego antes despues favor porfa
""".split())


def terminos_intencion(texto, tope=6):
    """Terminos que discriminan de un texto libre. Nunca lanza.

    Patron unicode: con [A-Za-z_] las tildes parten la palabra por la mitad
    ("migracion" -> "migraci"), que en un repo en espanol arruina la consulta.
    Los identificadores (con _ o .) van primero porque discriminan mucho mas
    que una palabra suelta.
    """
    try:
        import re
        out, vistas = [], set()
        for t in re.findall(r"[^\W\d_][\w.]{2,}", texto or "", re.UNICODE):
            bajo = t.lower()
            if bajo in VACIAS_INTENCION or bajo in vistas or len(bajo) < 4:
                continue
            vistas.add(bajo)
            out.append(t)
        out.sort(key=lambda t: (("_" not in t and "." not in t), -len(t)))
        return out[:tope]
    except Exception:
        return []


def vault_prompt_hits(texto, min_aciertos=2, tope=4, tope_sesiones=2):
    """Historico del vault relevante para un texto libre, o None.

    Es el nucleo de vault-prompt-search.py, que en Claude Code corre como
    UserPromptSubmit. jcode no expone el texto del prompt a ningun hook, asi
    que aqui se alimenta de la intencion aproximada por los argumentos de la
    primera herramienta del turno. Conservador a proposito: un acierto suelto
    es mas ruido que ayuda.
    """
    try:
        palabras = terminos_intencion(texto)
        if not palabras:
            return None
        scripts = os.path.join(VAULT_HOME, "scripts")
        if not os.path.isdir(scripts):
            scripts = os.path.expanduser("~/.claude/scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import vault_lookup as L
        proyecto, vault = L.resolver_vault()
        consulta = " OR ".join(f'"{p}"' for p in palabras)
        try:
            obs = L.buscar_observaciones(proyecto, vault, consulta, tope,
                                         crudo=True)
        except Exception:
            obs = []
        # La sesion contesta a otra pregunta: no "que fichero se toco" sino
        # "esto ya se intento y esto se aprendio". Ahi un acierto ya vale.
        try:
            ses = L.buscar_sesiones(proyecto, vault, consulta, tope_sesiones,
                                    crudo=True)
        except Exception:
            ses = []
        # Una sesion suelta sin ninguna observacion suele ser un saludo o una
        # coincidencia de cortesia, no trabajo real sobre el tema.
        if len(obs) < min_aciertos and len(ses) < 2:
            return None
        lineas = [
            f"[vault] El historico de {proyecto} tiene material sobre esto. "
            "Mira esto antes de investigar desde cero:"
        ]
        lineas += [f"  {oid}  {fecha}  {titulo[:88]}"
                   for oid, titulo, fecha in obs]
        if ses:
            lineas.append("  Sesiones que ya trabajaron esto:")
            for sid, titulo, fecha in ses:
                lineas.append(f"    {fecha}  {titulo[:72]}")
                lineas.append(f"      abrir: vault_lookup.py --ses {sid}")
        lineas.append(
            f"  Abrir: python3 {os.path.join(scripts, 'vault_lookup.py')} "
            "--obs <id>")
        return "\n".join(lineas)
    except SystemExit:
        return None
    except Exception:
        return None


def _turn_marker(session_id):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in
                   (session_id or "jcode"))[:120]
    return os.path.join(state_dir(), f"vault-turn-{safe}")


def turn_already_probed(session_id, arm=True):
    """True si en este turno ya se busco intencion en el vault.

    jcode no marca el inicio de turno, pero turn_end si marca el final, y
    borra este fichero. Asi la primera herramienta despues de un turn_end es
    la primera del turno nuevo, que es el momento mas parecido a
    UserPromptSubmit al que se puede llegar sin parchear jcode.
    """
    try:
        marca = _turn_marker(session_id)
        if os.path.exists(marca):
            return True
        if arm:
            os.makedirs(state_dir(), exist_ok=True)
            open(marca, "w").close()
        return False
    except Exception:
        return True  # ante la duda, no inyectar


def clear_turn_marker(session_id):
    try:
        os.remove(_turn_marker(session_id))
    except OSError:
        pass
    except Exception:
        pass


def vault_nudge(path):
    """Historico del vault sobre un fichero, o None. Nunca lanza.

    Es el nucleo de vault-read-nudge.py, que en Claude Code corre como su
    propio PreToolUse. jcode solo admite un pre_tool, asi que en vez de
    competir por ese hueco se expone aqui para que el gate lo encadene.
    """
    try:
        if not path or not path.endswith(
                (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".md",
                 ".sh", ".rs", ".go", ".yaml", ".yml", ".toml")):
            return None
        scripts = os.path.join(VAULT_HOME, "scripts")
        if not os.path.isdir(scripts):
            scripts = os.path.expanduser("~/.claude/scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import vault_lookup as L
        _, vault = L.resolver_vault(os.path.dirname(path) or None)
        seg = path.strip("/").split("/")
        hits = []
        for n in range(min(4, len(seg)), 0, -1):
            hits = L.buscar(vault, "/".join(seg[-n:]))
            if len(hits) == 1:
                break
        if len(hits) != 1:
            return None
        import re
        texto = open(hits[0][1], errors="replace").read()
        entradas = [
            m for m in (
                re.match(r"- \[\[obs-(\d+)\]\] — (.*)", ln.strip())
                for ln in L.seccion(texto, "Memoria").splitlines())
            if m
        ]
        if not entradas:
            return None
        base = os.path.basename(path)
        lineas = [
            f"[vault] {len(entradas)} observaciones sobre {base}. "
            "Las mas recientes:"
        ]
        lineas += [f"  {m.group(1)}  {m.group(2)[:88]}" for m in entradas[:3]]
        lineas.append(
            f"Amplia con: python3 {os.path.join(scripts, 'vault_lookup.py')} "
            f"{base}   (o --obs <id>)")
        return "\n".join(lineas)
    except SystemExit:
        return None
    except Exception:
        return None


def refresh_overlay(min_age_s=600):
    """Reescribe el overlay si ya esta rancio. Devuelve True si lo refresco.

    El overlay se relee en cada turno (verificado: cambiarlo a mitad de
    sesion cambia lo que ve el modelo), asi que refrescarlo desde turn_end
    mantiene el contexto del vault al dia sin reiniciar la sesion, y hace
    que /clear recupere una foto reciente en vez de la del arranque.
    """
    try:
        cwd = os.environ.get("JCODE_HOOK_CWD") or os.getcwd()
        dst = os.path.join(cwd, ".jcode", "prompt-overlay.md")
        try:
            if time.time() - os.path.getmtime(dst) < min_age_s:
                return False
        except OSError:
            pass
        cmd_context()
        return True
    except Exception:
        return False


def cmd_track():
    """pre_tool: registra el fichero y deja pasar siempre (exit 0)."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = payload
        track_tool_file(
            os.environ.get("JCODE_HOOK_SESSION_ID", "jcode"),
            payload.get("tool_name") or os.environ.get(
                "JCODE_HOOK_TOOL_NAME", ""),
            tool_input,
        )
    except Exception:
        pass
    return 0


def worker_path():
    """Ruta al worker de claude-mem, versión instalada más alta."""
    pattern = os.path.expanduser(
        "~/.claude/plugins/cache/thedotmack/claude-mem"
        "/*/scripts/worker-service.cjs"
    )
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else None


def additional_context(raw):
    """Extrae hookSpecificOutput.additionalContext de la salida de un hook."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    block = data.get("hookSpecificOutput") or {}
    return block.get("additionalContext") or ""


def build_overlay(vault_txt, mem_txt):
    """Compone el overlay. Orden fijo: vault primero, claude-mem después."""
    parts = []
    if vault_txt and vault_txt.strip():
        parts.append("## Vault del proyecto\n\n" + vault_txt.strip())
    if mem_txt and mem_txt.strip():
        parts.append("## Memoria de sesiones\n\n" + mem_txt.strip())
    if not parts:
        return ""
    return "# Contexto recuperado\n\n" + "\n\n".join(parts) + "\n"


def claude_mem(event, payload, timeout=TIMEOUT):
    """Invoca claude-mem en la plataforma raw. Devuelve stdout o None."""
    worker = worker_path()
    if not worker:
        return None
    try:
        proc = subprocess.run(
            ["bun", worker, "hook", "raw", event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    return proc.stdout


def run_vault_hook(payload):
    """Invoca vault-session-start.py del paquete instalado."""
    hook = os.path.join(VAULT_HOME, "hooks", "vault-session-start.py")
    if not os.path.exists(hook):
        return None
    env = dict(os.environ, GRAFT_MEM_VAULT_HOME=VAULT_HOME)
    try:
        proc = subprocess.run(
            [sys.executable, hook],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
    except Exception:
        return None
    return proc.stdout


def write_overlay(cwd, body):
    """Escribe el overlay de forma atómica. Cuerpo vacío = no tocar nada."""
    if not body or not body.strip():
        return False
    directory = os.path.join(cwd, ".jcode")
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, "prompt-overlay.md.tmp")
        dst = os.path.join(directory, "prompt-overlay.md")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, dst)
    except Exception:
        return False
    return True


def cmd_context():
    cwd = os.environ.get("JCODE_HOOK_CWD") or os.getcwd()
    payload = {
        "cwd": cwd,
        "session_id": os.environ.get("JCODE_HOOK_SESSION_ID", "jcode"),
        "source": "startup",
    }
    vault_txt = additional_context(run_vault_hook(payload))
    mem_txt = additional_context(claude_mem("context", payload))
    write_overlay(cwd, build_overlay(vault_txt, mem_txt))
    return 0


def cmd_passthrough(event):
    """Reenvía el evento de escritura a claude-mem con el payload de jcode."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    payload.setdefault("cwd", os.environ.get("JCODE_HOOK_CWD") or os.getcwd())
    payload.setdefault(
        "session_id", os.environ.get("JCODE_HOOK_SESSION_ID", "jcode")
    )
    if os.environ.get("JCODE_HOOK_TOOL_NAME"):
        payload.setdefault("tool_name", os.environ["JCODE_HOOK_TOOL_NAME"])

    # post_tool no trae el input de la herramienta, así que se adjuntan los
    # ficheros que pre_tool fue registrando. Sin esto, claude-mem graba la
    # observación con files_read/files_modified vacíos y el vault la filtra.
    leidos, tocados = drain_tracked_files(payload.get("session_id"))
    if leidos or tocados:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_input.setdefault("file_path", (tocados or leidos)[0])
        payload["tool_input"] = tool_input
        if leidos:
            payload.setdefault("files_read", leidos)
        if tocados:
            payload.setdefault("files_modified", tocados)

    timeout = SUMMARIZE_TIMEOUT if event == "summarize" else TIMEOUT
    claude_mem(event, payload, timeout=timeout)
    return 0


def cmd_turn_summarize(session_id):
    """Envia el resumen del turno a claude-mem.

    turn_end no trae tool_input en stdin (es el evento de cierre de turno,
    no de herramienta), asi que el payload sale de las envs que jcode ya
    expone para este evento: JCODE_HOOK_STATUS, JCODE_HOOK_LAST_ASSISTANT_TEXT.
    Solo tiene sentido si el turno fue ok y dejo texto de respuesta; si no,
    claude-mem lo descartaria igualmente (ver Stop hook: sin
    last_assistant_message no genera resumen).
    """
    if os.environ.get("JCODE_HOOK_STATUS") != "ok":
        return
    last_text = os.environ.get("JCODE_HOOK_LAST_ASSISTANT_TEXT", "")
    if not last_text.strip():
        return
    payload = {
        "cwd": os.environ.get("JCODE_HOOK_CWD") or os.getcwd(),
        "session_id": session_id,
        "last_assistant_message": last_text,
    }
    claude_mem("summarize", payload, timeout=SUMMARIZE_TIMEOUT)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--context":
        return cmd_context()
    if mode == "--summarize":
        return cmd_passthrough("summarize")
    if mode == "--observation":
        return cmd_passthrough("observation")
    if mode == "--track":
        return cmd_track()
    if mode == "--turn-end":
        # turn_end es observador: cierra el turno para que la siguiente
        # herramienta vuelva a poder consultar intencion, refresca el
        # overlay si esta rancio, y ahora tambien envia el resumen del
        # turno a claude-mem (jcode no tiene Stop; turn_end es su
        # equivalente real, y ya trae last_assistant_message).
        session_id = os.environ.get("JCODE_HOOK_SESSION_ID", "jcode")
        clear_turn_marker(session_id)
        refresh_overlay()
        cmd_turn_summarize(session_id)
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Un hook que revienta bloquea la sesión entera. Siempre 0.
        sys.exit(0)
