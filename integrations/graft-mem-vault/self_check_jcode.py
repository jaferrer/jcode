#!/usr/bin/env python3
"""Contract checks para el puente de jcode."""

import os
import runpy
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRIDGE = ROOT / "hooks" / "jcode-vault-bridge.py"


def test_additional_context_extraction():
    ns = runpy.run_path(str(BRIDGE))
    extract = ns["additional_context"]

    assert extract('{"hookSpecificOutput":{"additionalContext":"hola"}}') == "hola"
    assert extract('{"systemMessage":"solo esto"}') == ""
    assert extract("no es json") == ""
    assert extract("") == ""
    assert extract(None) == ""


def test_build_overlay_order_and_headers():
    ns = runpy.run_path(str(BRIDGE))
    build = ns["build_overlay"]

    out = build("del vault", "de la memoria")
    assert out.index("del vault") < out.index("de la memoria"), out
    assert "## Vault del proyecto" in out
    assert "## Memoria de sesiones" in out

    assert build("", "") == ""
    assert "## Memoria de sesiones" not in build("solo vault", "")


def test_write_overlay_is_atomic_and_creates_dir():
    ns = runpy.run_path(str(BRIDGE))
    write_overlay = ns["write_overlay"]

    with tempfile.TemporaryDirectory() as tmp:
        assert write_overlay(tmp, "# contexto\n") is True
        dst = Path(tmp) / ".jcode" / "prompt-overlay.md"
        assert dst.read_text() == "# contexto\n"
        assert not (Path(tmp) / ".jcode" / "prompt-overlay.md.tmp").exists()


def test_empty_body_preserves_existing_overlay():
    ns = runpy.run_path(str(BRIDGE))
    write_overlay = ns["write_overlay"]

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / ".jcode"
        directory.mkdir()
        dst = directory / "prompt-overlay.md"
        dst.write_text("# contexto viejo\n")
        assert write_overlay(tmp, "   \n") is False
        assert dst.read_text() == "# contexto viejo\n"


def test_bridge_exports_main():
    ns = runpy.run_path(str(BRIDGE))
    assert callable(ns.get("main")), "el puente no expone main callable"


def test_main_always_exits_zero():
    import subprocess as sp

    for flag in ("--context", "--summarize", "--observation", "--desconocido", ""):
        args = [str(BRIDGE)] + ([flag] if flag else [])
        result = sp.run(
            ["python3"] + args,
            input="entrada que no es json",
            capture_output=True,
            text=True,
            timeout=90,
            cwd="/tmp",
        )
        assert result.returncode == 0, \
            f"{flag!r} salió con {result.returncode}: {result.stderr}"


def test_bridge_tracks_and_attaches_tool_files():
    """post_tool de jcode no expone el input de la herramienta.

    Verificado empíricamente con una sonda: en `post_tool` solo llegan
    JCODE_HOOK_TOOL_NAME, STATUS, DURATION_MS y OUTPUT_BYTES, y stdin viene
    vacío. El `file_path` solo existe en `pre_tool` (por stdin).

    Sin ese dato, claude-mem graba las observaciones con files_read y
    files_modified vacíos, y el vault las descarta: `fetch_rows` en
    graft_mem_vault.py filtra por
    `files_modified NOT IN ('', '[]') OR files_read NOT IN ('', '[]')`.
    Ese es el motivo de que ninguna sesión jcode aparezca en el vault
    aunque el puente sí escriba en la base de datos.

    El puente registra los ficheros en `pre_tool` y los adjunta en la
    siguiente observación.
    """
    ns = runpy.run_path(str(BRIDGE))
    track = ns.get("track_tool_file")
    drain = ns.get("drain_tracked_files")
    assert callable(track), "el puente no expone track_tool_file"
    assert callable(drain), "el puente no expone drain_tracked_files"

    old_home = os.environ.get("GRAFT_MEM_VAULT_STATE")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GRAFT_MEM_VAULT_STATE"] = tmp
            ses = "session_test_1"

            # Read registra lectura; Edit/Write registran modificación.
            track(ses, "read", {"file_path": "/proj/leido.py"})
            track(ses, "edit", {"file_path": "/proj/tocado.py"})
            track(ses, "bash", {"command": "ls"})  # sin fichero: se ignora

            leidos, tocados = drain(ses)
            assert "/proj/leido.py" in leidos, leidos
            assert "/proj/tocado.py" in tocados, tocados
            # un fichero editado no se cuenta además como leído
            assert "/proj/tocado.py" not in leidos, leidos

            # drain vacía: la siguiente observación no repite los mismos
            assert drain(ses) == ([], [])

            # el rastreo está aislado por sesión
            track("session_test_2", "read", {"file_path": "/otra/x.py"})
            assert drain(ses) == ([], [])
            assert drain("session_test_2")[0] == ["/otra/x.py"]
    finally:
        if old_home is None:
            os.environ.pop("GRAFT_MEM_VAULT_STATE", None)
        else:
            os.environ["GRAFT_MEM_VAULT_STATE"] = old_home


def test_bridge_exposes_nudge_and_overlay_refresh():
    """Las dos piezas que en Claude Code son hooks propios.

    jcode no tiene hook de envio de prompt y solo admite un pre_tool, ya
    ocupado por el gate de tldr/rtk, asi que vault-read-nudge.py no puede
    instalarse tal cual. Su nucleo se expone aqui como vault_nudge() para
    que el gate lo encadene al bloqueo de lectura que ya iba a ocurrir.

    refresh_overlay() cubre el otro agujero: /clear no reemite
    session_start (los unicos source son create/attach/resume), pero el
    overlay se relee en cada turno, asi que refrescarlo desde turn_end
    mantiene el contexto fresco sin reiniciar la sesion.
    """
    ns = runpy.run_path(str(BRIDGE))
    nudge = ns.get("vault_nudge")
    refresh = ns.get("refresh_overlay")
    assert callable(nudge), "el puente no expone vault_nudge"
    assert callable(refresh), "el puente no expone refresh_overlay"

    # Fail-open: rutas imposibles o sin vault no revientan ni inventan texto.
    assert nudge("") is None
    assert nudge("/no/existe/x.py") is None
    assert nudge("/tmp/sin-extension-conocida.bin") is None

    # El throttle respeta un overlay reciente y no lo reescribe.
    with tempfile.TemporaryDirectory() as tmp:
        jd = os.path.join(tmp, ".jcode")
        os.makedirs(jd)
        dst = os.path.join(jd, "prompt-overlay.md")
        with open(dst, "w") as fh:
            fh.write("# reciente\n")
        antes = os.path.getmtime(dst)
        old = os.environ.get("JCODE_HOOK_CWD")
        os.environ["JCODE_HOOK_CWD"] = tmp
        try:
            assert refresh(min_age_s=3600) is False, \
                "refresco un overlay que aun estaba fresco"
            assert os.path.getmtime(dst) == antes
            assert open(dst).read() == "# reciente\n"
        finally:
            if old is None:
                os.environ.pop("JCODE_HOOK_CWD", None)
            else:
                os.environ["JCODE_HOOK_CWD"] = old


def test_intent_search_terms_and_once_per_turn():
    """Equivalente aproximado de vault-prompt-search.py en jcode.

    jcode no entrega el prompt del usuario a ningun hook, asi que la
    intencion se aproxima con los argumentos de la primera herramienta del
    turno. Dos invariantes importan: que el extractor no destroce el
    espanol acentuado, y que el aviso salga una sola vez por turno.
    """
    ns = runpy.run_path(str(BRIDGE))
    terminos = ns.get("terminos_intencion")
    probed = ns.get("turn_already_probed")
    clear = ns.get("clear_turn_marker")
    assert callable(terminos), "el puente no expone terminos_intencion"
    assert callable(probed), "el puente no expone turn_already_probed"
    assert callable(clear), "el puente no expone clear_turn_marker"

    # El patron [A-Za-z_] partia las palabras por la tilde: "migracion" se
    # quedaba en "migraci" y "disenada" en "dise", que arruina la consulta
    # en un repo escrito en espanol.
    t = terminos("como esta disenada la migración de módulos")
    assert "migración" in t, t
    assert "módulos" in t, t
    assert not any(x in t for x in ("migraci", "dise", "dulos")), t

    # Palabras vacias y saludos no deben generar consulta.
    assert terminos("hola que tal") == []
    assert terminos("") == []

    # Los identificadores mandan sobre las palabras sueltas.
    t2 = terminos("revisar fetch_rows en el modulo de sesiones")
    assert t2[0] == "fetch_rows", t2

    old = os.environ.get("GRAFT_MEM_VAULT_STATE")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GRAFT_MEM_VAULT_STATE"] = tmp
            ses = "ses_turno"
            assert probed(ses) is False, "el primer tool del turno debe pasar"
            assert probed(ses) is True, "el segundo tool no debe reconsultar"
            assert probed(ses) is True
            # turn_end cierra el turno: el siguiente vuelve a poder avisar.
            clear(ses)
            assert probed(ses) is False, \
                "tras turn_end deberia poder avisar de nuevo"
            # el marcador es por sesion
            assert probed("otra_ses") is False
    finally:
        if old is None:
            os.environ.pop("GRAFT_MEM_VAULT_STATE", None)
        else:
            os.environ["GRAFT_MEM_VAULT_STATE"] = old


def test_install_is_idempotent_and_config_parses():
    import subprocess as sp
    try:
        import tomllib
    except ModuleNotFoundError:
        class TomllibCompat:
            @staticmethod
            def loads(text):
                import json

                result = sp.run(
                    ["python3.12", "-c", (
                        "import json, sys, tomllib; "
                        "json.dump(tomllib.loads(sys.stdin.read()), sys.stdout)"
                    )],
                    input=text,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout)

        tomllib = TomllibCompat()

    install = ROOT / "install-jcode.sh"
    with tempfile.TemporaryDirectory() as home:
        jcode_home = Path(home) / ".jcode"
        jcode_home.mkdir()
        (jcode_home / "config.toml").write_text(
            "[display]\nemoji = false\n\n[hooks]\npre_tool_timeout_ms = 5000\n"
        )
        env = dict(os.environ, HOME=home, JCODE_HOME=str(jcode_home))

        for run in (1, 2):
            result = sp.run(
                ["bash", str(install)], env=env,
                capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 0, \
                f"pasada {run}: {result.stdout}{result.stderr}"

        text = (jcode_home / "config.toml").read_text()
        assert text.count("[hooks]") == 1, "duplicó la sección [hooks]"
        assert text.count("session_start") == 1, "duplicó session_start"

        config = tomllib.loads(text)
        hooks = config["hooks"]
        assert hooks["pre_tool_timeout_ms"] == 5000, "perdió una clave previa"
        for key in ("session_start", "session_end", "post_tool"):
            assert "jcode-vault-bridge.py" in hooks[key], key

        pkg = jcode_home / "graft-mem-vault"
        assert (pkg / "hooks" / "jcode-vault-bridge.py").exists()
        assert (pkg / "hooks" / "vault-session-start.py").exists()
        assert (pkg / "scripts" / "vault_lookup.py").exists()
        assert (pkg / "scripts" / "vault_scout.py").exists()


def test_mcp_server_handshake_and_tools():
    import json as js
    import subprocess as sp

    server = ROOT / "mcp" / "vault_mcp_server.py"
    requests = "\n".join([
        js.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05"}}),
        js.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        js.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"

    result = sp.run(["python3", str(server)], input=requests,
                    capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    replies = [js.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(replies) == 2, f"esperaba 2 respuestas, hubo {len(replies)}"

    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[0]["result"]["serverInfo"]["name"] == "vault"

    names = {tool["name"] for tool in replies[1]["result"]["tools"]}
    assert names == {"vault_lookup", "vault_scout"}, names


def test_mcp_malformed_line_after_valid_request_is_ignored():
    import json as js
    import subprocess as sp

    server = ROOT / "mcp" / "vault_mcp_server.py"
    requests = "\n".join([
        js.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05"}}),
        "{malformed json",
    ]) + "\n"

    result = sp.run(["python3", str(server)], input=requests,
                    capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", f"stderr contaminado: {result.stderr!r}"

    replies = [js.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(replies) == 1, f"esperaba solo initialize, hubo {len(replies)}: {replies}"
    assert replies[0]["id"] == 7
    assert "result" in replies[0]
    assert "error" not in replies[0]


def test_mcp_script_dir_resolution_prefers_env_then_jcode_then_claude():
    ns = runpy.run_path(str(ROOT / "mcp" / "vault_mcp_server.py"))
    scripts_dir = ns["scripts_dir"]

    old_env = os.environ.get("GRAFT_MEM_VAULT_SCRIPTS")
    old_pkg = os.environ.get("GRAFT_MEM_VAULT_HOME")
    old_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env_dir = home / "env-scripts"
            pkg_dir = home / "pkg" / "scripts"
            jcode_dir = home / ".jcode" / "graft-mem-vault" / "scripts"
            codex_dir = home / ".codex" / "graft-mem-vault" / "scripts"
            claude_dir = home / ".claude" / "scripts"
            env_dir.mkdir()
            pkg_dir.mkdir(parents=True)
            jcode_dir.mkdir(parents=True)
            codex_dir.mkdir(parents=True)
            claude_dir.mkdir(parents=True)

            os.environ["HOME"] = str(home)
            os.environ.pop("GRAFT_MEM_VAULT_HOME", None)
            os.environ["GRAFT_MEM_VAULT_SCRIPTS"] = str(env_dir)
            assert scripts_dir() == env_dir

            # GRAFT_MEM_VAULT_HOME es lo que exporta install-codex.sh; manda
            # sobre las rutas por defecto de cualquier harness.
            os.environ.pop("GRAFT_MEM_VAULT_SCRIPTS")
            os.environ["GRAFT_MEM_VAULT_HOME"] = str(pkg_dir.parent)
            assert scripts_dir() == pkg_dir

            os.environ.pop("GRAFT_MEM_VAULT_HOME")
            assert scripts_dir() == jcode_dir

            # Host solo-Codex: sin jcode instalado debe caer en ~/.codex,
            # no en ~/.claude. Con el fallback antiguo esto se enmascaraba
            # en máquinas que tuvieran ~/.claude/scripts poblado.
            jcode_dir.rmdir()
            assert scripts_dir() == codex_dir

            codex_dir.rmdir()
            assert scripts_dir() == claude_dir
    finally:
        for key, old in (
            ("GRAFT_MEM_VAULT_SCRIPTS", old_env),
            ("GRAFT_MEM_VAULT_HOME", old_pkg),
            ("HOME", old_home),
        ):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def test_mcp_tools_call_uses_configured_scripts_without_real_vault():
    import json as js
    import subprocess as sp

    server = ROOT / "mcp" / "vault_mcp_server.py"
    with tempfile.TemporaryDirectory() as tmp:
        scripts = Path(tmp)
        (scripts / "vault_lookup.py").write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('lookup:' + sys.argv[1])\n"
        )
        requests = "\n".join([
            js.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "vault_lookup",
                                 "arguments": {"termino": "sin-vault-real"}}}),
        ]) + "\n"
        env = dict(os.environ, GRAFT_MEM_VAULT_SCRIPTS=str(scripts))
        result = sp.run(["python3", str(server)], input=requests,
                        capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    reply = js.loads(result.stdout)
    assert reply["result"]["content"][0]["text"] == "lookup:sin-vault-real\n"


if __name__ == "__main__":
    test_additional_context_extraction()
    test_build_overlay_order_and_headers()
    test_write_overlay_is_atomic_and_creates_dir()
    test_empty_body_preserves_existing_overlay()
    test_bridge_exports_main()
    test_main_always_exits_zero()
    test_bridge_tracks_and_attaches_tool_files()
    test_bridge_exposes_nudge_and_overlay_refresh()
    test_intent_search_terms_and_once_per_turn()
    test_install_is_idempotent_and_config_parses()
    test_mcp_server_handshake_and_tools()
    test_mcp_malformed_line_after_valid_request_is_ignored()
    test_mcp_script_dir_resolution_prefers_env_then_jcode_then_claude()
    test_mcp_tools_call_uses_configured_scripts_without_real_vault()
    print(
        "OK — overlay, puente, rastreo de ficheros para el vault, "
        "instalador y servidor MCP (14 checks)"
    )
