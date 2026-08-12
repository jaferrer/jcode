#!/usr/bin/env python3
"""
jcode pre_tool gate — porta a jcode los hooks de tldr/rtk de Claude Code.

Claude Code usa tres hooks PreToolUse con matchers y capacidad de reescritura
(`updatedInput`):

  Read  -> tldr-read.mjs          inyecta nav map + trunca lecturas grandes
  Grep  -> tldr-nudge.mjs         deny+redirect one-shot si el patrón es símbolo
  Grep  -> smart-search-router.mjs
  Bash  -> rtk-rewrite.sh         reescribe el comando a su equivalente rtk
  Bash  -> tldr-nudge.mjs         aviso advisory

jcode tiene UN solo hook pre_tool, sin matchers, y su contrato es binario:
  exit 0 -> permite      exit 2 -> bloquea, stderr vuelve al modelo como error
No existe `updatedInput`, así que la reescritura automática es imposible.
La estrategia equivalente es "deny + instrucción exacta": bloqueamos una vez y
devolvemos por stderr el comando concreto que el modelo debe ejecutar. Como el
stderr llega al modelo, el efecto neto es el mismo (usar tldr/rtk) a costa de
un turno extra. Un marcador one-shot con TTL garantiza el fallback: repetir la
misma llamada pasa siempre, de modo que nunca se puede quedar atascado.

Modos (JCODE_TLDR_RTK_MODE o ~/.jcode/state/tldr-rtk-mode):
  off      — no hace nada
  observe  — nunca bloquea, solo registra en el log (DEFECTO seguro)
  enforce  — bloquea una vez y redirige

Allowlist por sesión: ~/.jcode/state/tldr-rtk-sessions.txt (un id por línea,
prefijo basta). Si existe y no está vacía, enforce solo aplica a esas sesiones;
el resto degrada a observe. Así una sesión puede activarlo sin afectar a otras
sesiones jcode que compartan el config global.

Fail-open por construcción: cualquier excepción => exit 0.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".jcode" / "state"
VAULT_BRIDGE = os.path.expanduser(
    "~/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py")
LOG = STATE / "tldr-rtk-activity.jsonl"
MARKERS = STATE / "tldr-rtk-markers"
MODE_FILE = STATE / "tldr-rtk-mode"
SESSIONS_FILE = STATE / "tldr-rtk-sessions.txt"
TTL_S = 120

CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".rb", ".php", ".swift", ".cs",
    ".scala", ".ex", ".exs", ".lua",
}

BYPASS = [
    re.compile(p) for p in (
        r"test_.*\.py$", r".*_test\.py$", r".*\.test\.[tj]sx?$",
        r".*\.spec\.[tj]sx?$", r".*_test\.go$", r".*_test\.rs$",
        r".*_spec\.rb$", r".*Tests?\.(kt|swift|cs)$", r".*_test\.exs?$",
        r"\.jcode/hooks/", r"\.claude/hooks/", r"\.claude/skills/",
        r"/node_modules/", r"/\.git/",
    )
]

LITERAL_MARKERS = {
    "TODO", "FIXME", "XXX", "HACK", "NOTE", "NOTES", "BUG", "WIP", "DEBUG",
    "REVIEW", "OPTIMIZE", "DEPRECATED", "WARNING", "ERROR", "INFO",
}

SIZE_THRESHOLD = 1500  # ~50 líneas


def log(entry):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with LOG.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def mode():
    m = os.environ.get("JCODE_TLDR_RTK_MODE", "").strip().lower()
    if not m:
        try:
            m = MODE_FILE.read_text().strip().lower()
        except Exception:
            m = "observe"
    return m if m in ("off", "observe", "enforce") else "observe"


def session_allowed(sid):
    """enforce solo si la allowlist está vacía/ausente o contiene esta sesión."""
    try:
        raw = SESSIONS_FILE.read_text()
    except Exception:
        return True
    ids = [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#")]
    if not ids:
        return True
    return any(sid.startswith(i) or i in sid for i in ids)


def marker_path(key):
    import hashlib
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return MARKERS / f"{h}.seen"


def recently_denied(key):
    """True si ya bloqueamos esta misma llamada hace poco => dejar pasar."""
    try:
        mp = marker_path(key)
        if not mp.exists():
            return False
        if time.time() - mp.stat().st_mtime > TTL_S:
            mp.unlink(missing_ok=True)
            return False
        mp.unlink(missing_ok=True)  # consumir: el reintento pasa
        return True
    except Exception:
        return False


def arm_marker(key):
    try:
        MARKERS.mkdir(parents=True, exist_ok=True)
        marker_path(key).write_text("")
    except Exception:
        pass


def have(binary):
    from shutil import which
    return which(binary) is not None


def deny(msg, meta):
    log({**meta, "action": "deny"})
    sys.stderr.write(msg)
    sys.exit(2)


def allow(meta=None):
    if meta:
        log(meta)
    sys.exit(0)


# ---------------------------------------------------------------- Read

def is_symbol(p):
    if not p or not isinstance(p, str):
        return False
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*([.:]{1,2}[A-Za-z0-9_]+)*", p):
        return False
    head = re.split(r"[.:]+", p)[0]
    if len(head) < 3:
        return False
    if p.upper() in LITERAL_MARKERS:
        return False
    return True


def format_nav_map(info, fname, budget=1400):
    """Nav map compacto al estilo del hook de Claude Code.

    jcode recorta el stderr del bloqueo a 2000 chars, así que el mapa se ciñe
    a `budget` y prioriza: clases > funciones > imports. Si no cabe todo, se
    indica cuántos elementos quedaron fuera para que el modelo sepa que hay
    más y pueda pedir el detalle con tldr.
    """
    lines = [f"# {fname}"]
    used = len(lines[0])
    omitted = 0

    def add(s):
        nonlocal used, omitted
        if used + len(s) + 1 > budget:
            omitted += 1
            return False
        lines.append(s)
        used += len(s) + 1
        return True

    classes = info.get("classes") or []
    if classes:
        add("## Classes")
        for c in classes:
            bases = c.get("bases") or []
            b = f" ({', '.join(bases)})" if bases else ""
            ln = c.get("line_number") or c.get("line") or "?"
            if not add(f"  {c.get('name')}{b}  [L{ln}]"):
                break
            for meth in (c.get("methods") or [])[:8]:
                mp = ", ".join(meth.get("params") or [])
                mln = meth.get("line_number") or meth.get("line") or "?"
                add(f"    .{meth.get('name')}({mp})  [L{mln}]")

    fns = info.get("functions") or []
    if fns:
        add("## Functions")
        for f in fns:
            p = ", ".join(f.get("params") or [])
            r = f" -> {f['return_type']}" if f.get("return_type") else ""
            a = "async " if f.get("is_async") else ""
            ln = f.get("line_number") or f.get("line") or "?"
            if not add(f"  {a}{f.get('name')}({p}){r}  [L{ln}]"):
                break

    if omitted:
        lines.append(f"  ... +{omitted} elementos más (usa tldr extract para el detalle)")
    return "\n".join(lines)


def _bridge():
    """Modulo del puente, o None. Nunca lanza."""
    try:
        spec = importlib.util.spec_from_file_location(
            "jcode_vault_bridge", VAULT_BRIDGE)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def intent_text(tool, ti):
    """Texto que aproxima la intencion del turno, a partir del primer tool.

    jcode no entrega el prompt del usuario a ningun hook, asi que lo mas
    cercano a UserPromptSubmit es lo que el modelo busca en su primer
    movimiento: el patron que grepea o el comando que lanza suelen llevar
    los terminos de la pregunta.
    """
    if tool in ("grep", "search", "agentgrep"):
        return " ".join(str(ti.get(k) or "") for k in
                        ("pattern", "query", "intent"))
    if tool == "bash":
        return " ".join(str(ti.get(k) or "") for k in ("intent", "command"))
    if tool in ("read", "notebookread"):
        # el nudge de fichero ya cubre esto mucho mejor
        return ""
    return str(ti.get("intent") or ti.get("query") or "")


def vault_intent(tool, sid, ti):
    """Historico del vault por intencion, una vez por turno. Nunca lanza."""
    try:
        texto = intent_text(tool, ti)
        if not texto.strip():
            return ""
        mod = _bridge()
        if mod is None:
            return ""
        if mod.turn_already_probed(sid):
            return ""
        return mod.vault_prompt_hits(texto) or ""
    except Exception:
        return ""


def vault_history(fp):
    """Historico del vault sobre el fichero, o cadena vacia. Nunca lanza.

    Reutiliza el nucleo de vault-read-nudge.py a traves del puente. En Claude
    Code ese aviso es su propio PreToolUse; aqui viaja pegado a un bloqueo que
    ya iba a ocurrir, asi que no cuesta ningun turno extra.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "jcode_vault_bridge", VAULT_BRIDGE)
        if spec is None or spec.loader is None:
            return ""
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.vault_nudge(fp) or ""
    except Exception:
        return ""


def handle_read(ti, m, sid):
    fp = ti.get("file_path") or ""
    if not fp:
        allow()
    ext = os.path.splitext(fp)[1]
    if ext not in CODE_EXT:
        allow()
    if any(b.search(fp) for b in BYPASS):
        allow()
    # lectura dirigida: ya sabe lo que busca
    if ti.get("offset") or (ti.get("limit") and int(ti.get("limit") or 0) < 100):
        allow()
    try:
        size = os.stat(fp).st_size
    except Exception:
        allow()
    if size < SIZE_THRESHOLD:
        allow()
    if not have("tldr"):
        allow()

    # ¿tldr extrae algo útil de este fichero?
    try:
        out = subprocess.run(
            ["tldr", "extract", fp], capture_output=True, text=True, timeout=6
        )
        info = json.loads(out.stdout)
    except Exception:
        allow()
    if not any(info.get(k) for k in ("functions", "classes")):
        allow()

    nfn = len(info.get("functions") or [])
    ncl = len(info.get("classes") or [])
    meta = {"tool": "read", "file": fp, "size": size, "fn": nfn, "cls": ncl,
            "mode": m, "session": sid}
    key = f"read:{fp}"
    if m != "enforce" or recently_denied(key):
        allow(meta)
    arm_marker(key)
    # jcode no admite additionalContext, así que el nav map va en el propio
    # stderr del bloqueo: el modelo lo recibe como texto del error y ya no
    # necesita ejecutar tldr aparte (ahorra el turno extra).
    nav = format_nav_map(info, os.path.basename(fp))
    hist = vault_history(fp)
    extra = f"\n\n{hist}" if hist else ""
    deny(
        f"Lectura completa evitada: {os.path.basename(fp)} son {size} bytes "
        f"(~{size // 4} tokens). Aquí tienes su mapa estructural, que es lo "
        f"que ibas a extraer del fichero:\n\n{nav}{extra}\n\n"
        f"Lee solo el rango que necesites con offset/limit, o profundiza con "
        f"`tldr calls <simbolo>` / `tldr impact <simbolo>`.\n"
        f"Si de verdad necesitas el fichero entero, repite exactamente esta "
        f"misma llamada Read y pasará sin bloqueo.",
        meta,
    )


# ---------------------------------------------------------------- Grep

def handle_grep(ti, m, sid):
    pat = ti.get("pattern") or ti.get("query") or ""
    if not is_symbol(pat):
        allow()
    if not have("tldr"):
        allow()
    meta = {"tool": "grep", "pattern": pat, "mode": m, "session": sid}
    key = f"grep:{pat}"
    if m != "enforce" or recently_denied(key):
        allow(meta)
    arm_marker(key)
    deny(
        f'"{pat}" parece un SÍMBOLO de código. tldr es más rápido y gasta '
        f"muchos menos tokens que grep:\n"
        f"  tldr search {pat} .     # dónde se define\n"
        f"  tldr calls {pat}        # quién lo llama\n"
        f"  tldr impact {pat}       # qué se rompe si cambia\n\n"
        f"Si buscas TEXTO LITERAL y no un símbolo, repite exactamente el "
        f"mismo grep y pasará sin bloqueo.",
        meta,
    )


# ---------------------------------------------------------------- Bash

GREP_FLAG_GUARD = re.compile(r"(^|\s)-[A-Za-z]*[coml][A-Za-z]*(\s|$)")
GREP_TOOL = re.compile(r"\b(grep|egrep|rg|ripgrep)\b")


def rtk_rewrite(cmd):
    """Delegamos toda la lógica de reescritura al binario rtk."""
    if not have("rtk"):
        return None
    # Guard verificado con rtk 0.37.2: `rtk grep` ignora -c/-o/-m/-l y rompe
    # el alcance de fichero único. Correcto > ahorro.
    if GREP_TOOL.search(cmd):
        if GREP_FLAG_GUARD.search(cmd):
            return None
        for w in cmd.split():
            w = w.strip("\"'")
            if w and os.path.isfile(w):
                return None
    try:
        out = subprocess.run(
            ["rtk", "rewrite", cmd], capture_output=True, text=True, timeout=5
        )
        new = (out.stdout or "").strip()
    except Exception:
        return None
    if not new or new == cmd.strip():
        return None
    return new


def handle_bash(ti, m, sid):
    cmd = (ti.get("command") or "").strip()
    if not cmd:
        allow()
    # nunca interceptar el propio tooling ni comandos ya optimizados
    if re.match(r"^\s*(rtk|tldr)\b", cmd):
        allow()

    new = rtk_rewrite(cmd)
    if not new:
        allow()
    meta = {"tool": "bash", "cmd": cmd[:200], "rewrite": new[:200],
            "mode": m, "session": sid}
    key = f"bash:{cmd}"
    if m != "enforce" or recently_denied(key):
        allow(meta)
    arm_marker(key)
    deny(
        f"rtk tiene una versión equivalente de este comando que gasta muchos "
        f"menos tokens.\n\nEn vez de:\n  {cmd}\n\nEjecuta:\n  {new}\n\n"
        f"Si necesitas la salida cruda del comando original, repítelo "
        f"exactamente igual y pasará sin bloqueo.",
        meta,
    )


# ---------------------------------------------------------------- main

def track_for_vault(tool, sid, ti):
    """Registra el fichero tocado para el puente del vault. Nunca lanza.

    El post_tool de jcode no expone el input de la herramienta, así que sin
    este registro las observaciones llegan a claude-mem sin ficheros y el
    vault las descarta. Corre siempre, aunque el gate esté en 'off'.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "jcode_vault_bridge", VAULT_BRIDGE)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.track_tool_file(sid, tool, ti)
    except Exception:
        pass


def main():
    m = mode()
    tool = (os.environ.get("JCODE_HOOK_TOOL_NAME") or "").strip().lower()
    sid = os.environ.get("JCODE_HOOK_SESSION_ID") or ""

    try:
        raw = sys.stdin.read()
    except Exception:
        sys.exit(0)
    try:
        ti = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)
    if not isinstance(ti, dict):
        sys.exit(0)
    # algunos payloads anidan la entrada
    if "tool_input" in ti and isinstance(ti["tool_input"], dict):
        ti = ti["tool_input"]

    # El rastreo para el vault es independiente del gate: corre en todos los
    # modos, incluido 'off', porque es la unica via por la que jcode puede
    # asociar ficheros a una observacion.
    track_for_vault(tool, sid, ti)

    if m == "off":
        sys.exit(0)

    if m == "enforce" and not session_allowed(sid):
        m = "observe"

    # El aviso por intencion va ANTES de los handlers: allow() hace
    # sys.exit(0), asi que cualquier cosa colocada despues solo se ejecutaria
    # en los casos que ya bloquean. Este es el equivalente de
    # UserPromptSubmit: mirar el historico antes de investigar, no despues.
    # Cuesta un turno, asi que solo con senal clara y una vez por turno.
    if m == "enforce":
        vh = vault_intent(tool, sid, ti)
        if vh:
            deny(
                f"{vh}\n\n"
                f"Repite exactamente la misma llamada para continuar con lo "
                f"que ibas a hacer.",
                {"tool": tool, "mode": m, "session": sid, "vault": "intent"},
            )

    if tool == "read":
        handle_read(ti, m, sid)
    elif tool in ("grep", "search", "agentgrep"):
        handle_grep(ti, m, sid)
    elif tool == "bash":
        handle_bash(ti, m, sid)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # fail-open siempre
