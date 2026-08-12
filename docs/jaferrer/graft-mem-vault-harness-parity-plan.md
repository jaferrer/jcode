# Paridad graft-mem-vault entre harnesses — Plan de implementación

> **Para trabajadores agénticos:** SUB-SKILL REQUERIDA: `superpowers:subagent-driven-development` (recomendada) o `superpowers:executing-plans` para implementar tarea por tarea. Los pasos usan casillas (`- [ ]`) para seguimiento.

**Objetivo:** que graft-mem-vault funcione en jcode igual que en Claude Code y Codex — inyección de contexto al arrancar, escritura de memoria al cerrar, y scout accesible desde los tres.

**Arquitectura:** jcode no puede inyectar contexto desde un hook, pero sí lee `.jcode/prompt-overlay.md` al ensamblar el prompt. Un puente nuevo recoge el `additionalContext` que ya producen claude-mem y `vault-session-start.py` y lo deposita en ese fichero. La escritura no necesita inyección, así que los hooks observadores de jcode bastan, delegando en la plataforma `raw` de claude-mem. Un servidor MCP expone el scout a los tres harnesses.

**Stack:** Python 3 (solo stdlib), bash, TOML, JSON-RPC sobre stdio.

**Spec de origen:** `docs/superpowers/specs/2026-08-11-graft-mem-vault-harness-parity-design.md`

---

## Cómo trabajar en este plan

Estas reglas son obligatorias. Están escritas porque el ejecutor no conoce este repositorio.

1. **No modifiques el núcleo.** `graft_mem_vault.py`, `vault_lookup.py`, `vault_scout.py`, `refresh_vaults.py` y los hooks existentes **no se tocan**. Si crees que necesitas cambiarlos, PARA y reporta: es señal de que el plan está mal.
2. **`~/.claude-mem/claude-mem.db` pesa 159 MB y son datos reales del usuario.** No lo borres, no lo muevas, no escribas en él sin haber hecho antes la copia de seguridad de la Tarea 1.
3. **Todo hook debe salir con código 0 siempre.** Un hook que revienta bloquea la sesión entera. Ante cualquier error: emitir `{}` y salir con 0.
4. **Pruebas:** este repo no usa pytest ni unittest. La convención es funciones `test_*()` con `assert` planos y un runner explícito al final:

   ```python
   if __name__ == "__main__":
       test_uno()
       test_dos()
       print("OK — descripción de lo verificado")
   ```

   Se ejecutan con `python3 self_check_jcode.py`. Sigue exactamente ese patrón; mira `self_check_codex.py` como referencia.
5. **Validación sintáctica** antes de cada commit: `bash -n fichero.sh` para bash, `python3 -m py_compile fichero.py` para Python.
6. **Documentación en español de España** (tildes, ñ, ¿?, ¡!). Nombres de variables y funciones en inglés. Mensajes de commit en inglés.
7. **Si un paso falla de forma que no esté prevista en el plan, PARA y reporta.** No improvises una solución alternativa.

## Restricciones globales

Valores exactos, copiados del spec. Aplican a todas las tareas.

- Plataforma de claude-mem para jcode: **`raw`** (no `claude-code`, no `codex`).
- Eventos de claude-mem usados: **`context`**, **`summarize`**, **`observation`**.
- Timeout de toda llamada a claude-mem: **5 segundos**.
- Overlay: **`.jcode/prompt-overlay.md`**, relativo al directorio del proyecto. Por proyecto, nunca global.
- Si el contenido recuperado viene vacío o falla la recuperación: **no tocar el overlay existente**. Nunca escribir un overlay vacío.
- Hooks de jcode disponibles: `session_start`, `session_end`, `turn_start`, `turn_end`, `pre_tool`, `post_tool`. Todos son observadores salvo `pre_tool`.
- Variables de entorno que jcode expone a los hooks: `JCODE_HOOK_SESSION_ID`, `JCODE_HOOK_CWD`, `JCODE_HOOK_PAYLOAD` (JSON, tope 16 KB), `JCODE_HOOK_TOOL_NAME`.
- Paquete de instalación en jcode: **`~/.jcode/graft-mem-vault/{scripts,hooks}`**, con `GRAFT_MEM_VAULT_HOME` apuntando ahí (mismo patrón que `install-codex.sh`).
- El orden de composición del overlay es fijo: **primero vault, después claude-mem**, cada uno bajo su encabezado. Sin deduplicar.
- `vault-read-nudge.py` **no se porta a jcode**. Está descartado conscientemente en el spec.

## Estructura de ficheros

**Se crean:**

| Fichero | Responsabilidad |
|---|---|
| `tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py` | El puente. Única pieza con lógica nueva. |
| `tricks_ferrer/graft-mem-vault/install-jcode.sh` | Instalador: copia scripts y cablea `~/.jcode/config.toml`. |
| `tricks_ferrer/graft-mem-vault/self_check_jcode.py` | Verificaciones de contrato del puente y del instalador. |
| `tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py` | Servidor MCP stdio que expone `vault_lookup` y `vault_scout`. |
| `docs/notas/claude-mem-raw-contract.md` | Contrato de escritura descubierto en la Tarea 1. |

**Se modifican:**

| Fichero | Cambio |
|---|---|
| `~/.jcode/config.toml` | Añadir sección `[hooks]`. |
| `~/.zshrc` | Añadir llamada al puente dentro de la función `jcode()` existente. |
| `.gitignore` | Añadir `.jcode/`. |
| `.mcp.json` | Registrar el servidor MCP del vault. |
| `tricks_ferrer/graft-mem-vault/README.md` | Documentar el soporte de jcode. |

---

## Tarea 1: Descubrir el contrato de escritura de `claude-mem hook raw`

El spec marca esto como incógnita. Nada se cablea hasta saber qué espera. **Esta tarea es de descubrimiento y termina en un documento, no en código.**

**Ficheros:**
- Crear: `docs/notas/claude-mem-raw-contract.md`

**Produce:** la forma exacta del JSON de stdin que esperan `raw summarize` y `raw observation`, que la Tarea 4 consumirá.

- [ ] **Paso 1: Copia de seguridad de la base de datos**

  ```bash
  cp ~/.claude-mem/claude-mem.db ~/.claude-mem/claude-mem.db.bak-parity
  ls -la ~/.claude-mem/claude-mem.db.bak-parity
  ```

  Esperado: un fichero de ~159 MB. Si el `cp` falla, PARA.

- [ ] **Paso 2: Registrar el estado inicial**

  ```bash
  sqlite3 ~/.claude-mem/claude-mem.db "SELECT name FROM sqlite_master WHERE type='table';" | head -20
  ```

  Anota los nombres de tabla. Los necesitarás en el paso 5 para comprobar si una escritura de prueba ha dejado rastro.

- [ ] **Paso 3: Buscar una variable de entorno que redirija el almacén**

  ```bash
  W=$(ls -d ~/.claude/plugins/cache/thedotmack/claude-mem/*/scripts/worker-service.cjs | tail -1)
  strings "$W" | grep -oE 'CLAUDE_MEM_[A-Z_]+' | sort -u
  ```

  Si aparece algo tipo `CLAUDE_MEM_HOME` o `CLAUDE_MEM_DB`, úsalo en el paso 4 para apuntar a un directorio temporal y evitar tocar el almacén real. Si no aparece nada, sigue con la copia de seguridad como única red.

- [ ] **Paso 4: Probar `raw observation` con un payload mínimo**

  ```bash
  W=$(ls -d ~/.claude/plugins/cache/thedotmack/claude-mem/*/scripts/worker-service.cjs | tail -1)
  echo '{"cwd":"/tmp/parity-probe","session_id":"parity-probe-1","tool_name":"Read","tool_input":{"file_path":"/tmp/parity-probe/x.txt"}}' \
    | timeout 30 bun "$W" hook raw observation 2>&1 | head -20
  echo "exit: $?"
  ```

  Anota literalmente qué devuelve y con qué código sale. Si se queja de un campo ausente, añádelo y repite — así se descubre el contrato.

- [ ] **Paso 5: Probar `raw summarize` igual**

  ```bash
  W=$(ls -d ~/.claude/plugins/cache/thedotmack/claude-mem/*/scripts/worker-service.cjs | tail -1)
  echo '{"cwd":"/tmp/parity-probe","session_id":"parity-probe-1"}' \
    | timeout 60 bun "$W" hook raw summarize 2>&1 | head -20
  echo "exit: $?"
  ```

  `summarize` puede tardar: llama a un modelo. Por eso el timeout es 60 aquí y no 5.

- [ ] **Paso 6: Comprobar si las pruebas han escrito algo**

  ```bash
  sqlite3 ~/.claude-mem/claude-mem.db \
    "SELECT COUNT(*) FROM sqlite_master;" 
  ls -la ~/.claude-mem/claude-mem.db
  ```

  Compara el tamaño y la fecha con el paso 1. Si ha crecido, la escritura funciona y ha dejado rastro con `session_id` `parity-probe-1` — anótalo para poder limpiarlo después.

- [ ] **Paso 7: Escribir el contrato**

  Crea `docs/notas/claude-mem-raw-contract.md` con: los campos exactos de stdin que acepta cada evento, la salida de cada uno, los códigos de salida, cuánto tarda `summarize`, y si hubo variable de entorno para redirigir el almacén. Sin conjeturas: solo lo observado.

- [ ] **Paso 8: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add docs/notas/claude-mem-raw-contract.md
  git commit -m "docs: record claude-mem raw platform write contract"
  ```

---

## Tarea 2: Puente — modo `--context`

**Ficheros:**
- Crear: `tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py`
- Crear: `tricks_ferrer/graft-mem-vault/self_check_jcode.py`

**Consume:** nada de tareas anteriores.
**Produce:** `worker_path()`, `additional_context(raw)`, `build_overlay(vault_txt, mem_txt)`, `cmd_context()`. La Tarea 3 y la 4 amplían este mismo fichero.

- [ ] **Paso 1: Escribir la prueba que falla**

  Crea `tricks_ferrer/graft-mem-vault/self_check_jcode.py`:

  ```python
  #!/usr/bin/env python3
  """Contract checks para el puente de jcode."""

  import runpy
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


  if __name__ == "__main__":
      test_additional_context_extraction()
      test_build_overlay_order_and_headers()
      print("OK — extracción y composición del overlay")
  ```

- [ ] **Paso 2: Ejecutar y verificar que falla**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: FALLA con `FileNotFoundError` sobre `jcode-vault-bridge.py`.

- [ ] **Paso 3: Escribir la implementación mínima**

  Crea `tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py`:

  ```python
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

  Nunca falla ruidosamente: siempre sale con 0.
  """

  import glob
  import json
  import os
  import subprocess
  import sys

  TIMEOUT = 5
  SUMMARIZE_TIMEOUT = 60

  VAULT_HOME = os.environ.get(
      "GRAFT_MEM_VAULT_HOME", os.path.expanduser("~/.jcode/graft-mem-vault")
  )


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
  ```

- [ ] **Paso 4: Ejecutar y verificar que pasa**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: `OK — extracción y composición del overlay`

- [ ] **Paso 5: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py \
          tricks_ferrer/graft-mem-vault/self_check_jcode.py
  git commit -m "feat: add jcode vault bridge with overlay composition"
  ```

---

## Tarea 3: Puente — escritura del overlay y resiliencia

Aquí vive la regla más importante del spec: **un fallo nunca debe dejar el overlay vacío**, porque un overlay vacío es indistinguible de «este proyecto no tiene memoria».

**Ficheros:**
- Modificar: `tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py`
- Modificar: `tricks_ferrer/graft-mem-vault/self_check_jcode.py`

**Consume:** `build_overlay`, `additional_context`, `worker_path` de la Tarea 2.
**Produce:** `claude_mem(event, payload, timeout)`, `run_vault_hook(payload)`, `write_overlay(cwd, body)`, `cmd_context()`.

- [ ] **Paso 1: Escribir las pruebas que fallan**

  Añade a `self_check_jcode.py`, antes del bloque `__main__`:

  ```python
  def test_write_overlay_is_atomic_and_creates_dir():
      import tempfile
      ns = runpy.run_path(str(BRIDGE))
      write_overlay = ns["write_overlay"]

      with tempfile.TemporaryDirectory() as tmp:
          write_overlay(tmp, "# contenido\n")
          dst = Path(tmp) / ".jcode" / "prompt-overlay.md"
          assert dst.exists(), "no creó .jcode/prompt-overlay.md"
          assert dst.read_text() == "# contenido\n"
          assert not (Path(tmp) / ".jcode" / "prompt-overlay.md.tmp").exists(), \
              "dejó el temporal sin renombrar"


  def test_empty_body_preserves_existing_overlay():
      import tempfile
      ns = runpy.run_path(str(BRIDGE))
      write_overlay = ns["write_overlay"]

      with tempfile.TemporaryDirectory() as tmp:
          write_overlay(tmp, "# contexto viejo\n")
          write_overlay(tmp, "")
          write_overlay(tmp, "   \n")
          dst = Path(tmp) / ".jcode" / "prompt-overlay.md"
          assert dst.read_text() == "# contexto viejo\n", \
              "un cuerpo vacío destruyó el overlay existente"
  ```

  Y añade las dos llamadas al runner:

  ```python
      test_write_overlay_is_atomic_and_creates_dir()
      test_empty_body_preserves_existing_overlay()
  ```

- [ ] **Paso 2: Ejecutar y verificar que falla**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: FALLA con `KeyError: 'write_overlay'`.

- [ ] **Paso 3: Implementar**

  Añade a `jcode-vault-bridge.py`, después de `build_overlay`:

  ```python
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
  ```

- [ ] **Paso 4: Ejecutar y verificar que pasa**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: `OK — extracción y composición del overlay`

- [ ] **Paso 5: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py \
          tricks_ferrer/graft-mem-vault/self_check_jcode.py
  git commit -m "feat: write jcode overlay atomically, preserve on empty result"
  ```

---

## Tarea 4: Puente — modos de escritura y punto de entrada

**Ficheros:**
- Modificar: `tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py`
- Modificar: `tricks_ferrer/graft-mem-vault/self_check_jcode.py`

**Consume:** el contrato documentado en `docs/notas/claude-mem-raw-contract.md` (Tarea 1) y `claude_mem()` de la Tarea 3.
**Produce:** `cmd_passthrough(event)`, `main()`.

> **Antes de empezar:** lee `docs/notas/claude-mem-raw-contract.md`. Si el contrato indica que `summarize` u `observation` necesitan campos que no están en el payload que se construye abajo, añádelos. Si el contrato dice que alguno de los dos eventos no funciona, PARA y reporta.

- [ ] **Paso 1: Escribir la prueba que falla**

  Añade a `self_check_jcode.py`:

  ```python
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
  ```

  Y al runner:

  ```python
      test_main_always_exits_zero()
  ```

- [ ] **Paso 2: Ejecutar y verificar que falla**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: FALLA — el script no tiene `main()` y no hace nada al ejecutarse.

- [ ] **Paso 3: Implementar**

  Añade al final de `jcode-vault-bridge.py`:

  ```python
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

      timeout = SUMMARIZE_TIMEOUT if event == "summarize" else TIMEOUT
      claude_mem(event, payload, timeout=timeout)
      return 0


  def main():
      mode = sys.argv[1] if len(sys.argv) > 1 else ""
      if mode == "--context":
          return cmd_context()
      if mode == "--summarize":
          return cmd_passthrough("summarize")
      if mode == "--observation":
          return cmd_passthrough("observation")
      return 0


  if __name__ == "__main__":
      try:
          sys.exit(main())
      except Exception:
          # Un hook que revienta bloquea la sesión entera. Siempre 0.
          sys.exit(0)
  ```

- [ ] **Paso 4: Ejecutar y verificar que pasa**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 -m py_compile hooks/jcode-vault-bridge.py
  python3 self_check_jcode.py
  ```

  Esperado: compila sin salida, y luego `OK — extracción y composición del overlay`

- [ ] **Paso 5: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add tricks_ferrer/graft-mem-vault/hooks/jcode-vault-bridge.py \
          tricks_ferrer/graft-mem-vault/self_check_jcode.py
  git commit -m "feat: add jcode bridge write passthrough and fail-open entrypoint"
  ```

---

## Tarea 5: Instalador `install-jcode.sh`

**Ficheros:**
- Crear: `tricks_ferrer/graft-mem-vault/install-jcode.sh`
- Modificar: `tricks_ferrer/graft-mem-vault/self_check_jcode.py`

**Consume:** el puente de las Tareas 2-4.
**Produce:** el paquete instalado en `~/.jcode/graft-mem-vault/` y la sección `[hooks]` cableada.

> **Aviso sobre TOML:** `~/.jcode/config.toml` **ya tiene** una sección `[hooks]` con `pre_tool_timeout_ms = 5000`. Hay que **añadir claves dentro de esa sección existente**, no crear una segunda — un TOML con `[hooks]` duplicado no parsea. La stdlib de Python lee TOML (`tomllib`) pero no lo escribe, así que la edición es por líneas. Debe ser idempotente: ejecutar el instalador dos veces no puede duplicar nada.

- [ ] **Paso 1: Escribir la prueba que falla**

  Añade a `self_check_jcode.py`:

  ```python
  def test_install_is_idempotent_and_config_parses():
      import shutil
      import subprocess as sp
      import tempfile
      import tomllib

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
  ```

  Añade `import os` arriba del fichero si aún no está, y la llamada al runner:

  ```python
      test_install_is_idempotent_and_config_parses()
  ```

- [ ] **Paso 2: Ejecutar y verificar que falla**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: FALLA porque `install-jcode.sh` no existe.

- [ ] **Paso 3: Implementar el instalador**

  Crea `tricks_ferrer/graft-mem-vault/install-jcode.sh`:

  ```bash
  #!/usr/bin/env bash
  # Instala graft-mem-vault para jcode.
  #
  # jcode no inyecta contexto desde hooks, así que el puente escribe
  # .jcode/prompt-overlay.md, que jcode sí monta en el prompt.
  set -euo pipefail

  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  JCODE_HOME="${JCODE_HOME:-$HOME/.jcode}"
  PACKAGE="$JCODE_HOME/graft-mem-vault"
  CONFIG="$JCODE_HOME/config.toml"
  BRIDGE="$PACKAGE/hooks/jcode-vault-bridge.py"

  ok()    { echo "✓ $1"; }
  falta() { echo "✗ $1" >&2; }

  mkdir -p "$PACKAGE/scripts" "$PACKAGE/hooks"

  cp "$DIR/graft_mem_vault.py" "$DIR/refresh_vaults.py" \
     "$DIR/vault_lookup.py" "$DIR/vault_scout.py" "$PACKAGE/scripts/"
  cp "$DIR/hooks/vault-session-start.py" \
     "$DIR/hooks/jcode-vault-bridge.py" "$PACKAGE/hooks/"
  chmod +x "$PACKAGE/hooks/jcode-vault-bridge.py"
  ok "scripts y hooks en $PACKAGE"

  if [ ! -f "$CONFIG" ]; then
      printf '[hooks]\n' > "$CONFIG"
      ok "config.toml creado"
  fi

  python3 - "$CONFIG" "$BRIDGE" <<'PY'
  import sys

  config_path, bridge = sys.argv[1], sys.argv[2]
  wanted = {
      "session_start": f'"{bridge} --context"',
      "session_end":   f'"{bridge} --summarize"',
      "post_tool":     f'"{bridge} --observation"',
  }

  with open(config_path) as handle:
      lines = handle.readlines()

  # Localizar la sección [hooks] y su final.
  start = None
  for index, line in enumerate(lines):
      if line.strip() == "[hooks]":
          start = index
          break

  if start is None:
      lines.append("\n[hooks]\n")
      start = len(lines) - 1
      end = len(lines)
  else:
      end = len(lines)
      for index in range(start + 1, len(lines)):
          if lines[index].startswith("["):
              end = index
              break

  block = lines[start + 1:end]
  existing = {
      line.split("=", 1)[0].strip()
      for line in block
      if "=" in line and not line.strip().startswith("#")
  }

  added = [
      f"{key} = {value}\n"
      for key, value in wanted.items()
      if key not in existing
  ]

  if added:
      # Insertar justo tras las claves ya presentes, antes de la línea en blanco
      # final, para no romper la separación entre secciones.
      tail = len(block)
      while tail > 0 and not block[tail - 1].strip():
          tail -= 1
      new_block = block[:tail] + added + block[tail:]
      lines[start + 1:end] = new_block
      with open(config_path, "w") as handle:
          handle.writelines(lines)
      print(f"✓ {len(added)} hook(s) cableados en [hooks]")
  else:
      print("✓ hooks ya cableados (sin cambios)")
  PY

  ok "instalación completa"
  echo
  echo "Falta un paso manual: añade la llamada al puente en la función jcode()"
  echo "de tu ~/.zshrc (ver Tarea 6 del plan)."
  ```

- [ ] **Paso 4: Validar sintaxis y ejecutar la prueba**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  bash -n install-jcode.sh
  python3 self_check_jcode.py
  ```

  Esperado: `bash -n` sin salida, y luego `OK — extracción y composición del overlay`

- [ ] **Paso 5: Instalar de verdad y comprobar el config real**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  cp ~/.jcode/config.toml ~/.jcode/config.toml.bak-parity
  bash install-jcode.sh
  python3 -c "import tomllib; print(tomllib.load(open('$HOME/.jcode/config.toml','rb'))['hooks'])"
  ```

  Esperado: un diccionario con `pre_tool_timeout_ms` **y** las tres claves nuevas. Si `tomllib` lanza excepción, restaura la copia (`cp ~/.jcode/config.toml.bak-parity ~/.jcode/config.toml`) y PARA.

- [ ] **Paso 6: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add tricks_ferrer/graft-mem-vault/install-jcode.sh \
          tricks_ferrer/graft-mem-vault/self_check_jcode.py
  git commit -m "feat: add idempotent jcode installer wiring bridge into config hooks"
  ```

---

## Tarea 6: Wrapper de zsh y `.gitignore`

El hook `session_start` es fire-and-forget y no está determinado que gane la carrera contra el ensamblado del prompt. El wrapper escribe el overlay **antes** de que jcode arranque, sin carrera posible. Los dos caminos son idempotentes y la redundancia es deliberada.

**Ficheros:**
- Modificar: `~/.zshrc` (función `jcode()` existente)
- Modificar: `.gitignore`

- [ ] **Paso 1: Inspeccionar la función actual**

  ```bash
  grep -n -A5 '^jcode()' ~/.zshrc
  ```

  Esperado: la función que sincroniza los combos de OmniRoute y llama a `command jcode "$@"`. **No la reemplaces entera**: solo vas a insertar una línea.

- [ ] **Paso 2: Insertar la llamada al puente**

  Edita `~/.zshrc` para que la función quede así — la línea nueva es la del puente, justo antes de `command jcode`:

  ```bash
  jcode() {
    "$HOME/.local/bin/omniroute-jcode-sync.sh"
    "$HOME/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py" --context 2>/dev/null
    command jcode "$@"
  }
  ```

- [ ] **Paso 3: Verificar que la función carga**

  ```bash
  zsh -ic 'type jcode' 2>&1 | tail -1
  ```

  Esperado: `jcode is a shell function from /Users/ferrer/.zshrc`

- [ ] **Paso 4: Comprobar que el overlay se genera**

  ```bash
  cd /Users/ferrer/guia
  ~/.jcode/graft-mem-vault/hooks/jcode-vault-bridge.py --context
  echo "exit: $?"
  ls -la .jcode/prompt-overlay.md && head -5 .jcode/prompt-overlay.md
  ```

  Esperado: exit 0 y un overlay con el encabezado `# Contexto recuperado`. Si el fichero no existe, revisa si `claude-mem` y `vault-session-start.py` devolvieron contenido — con un proyecto sin memoria es legítimo que no haya overlay.

- [ ] **Paso 5: Añadir `.jcode/` a `.gitignore`**

  ```bash
  cd /Users/ferrer/guia
  grep -qxF '.jcode/' .gitignore || printf '\n# Overlay generado del vault (jcode)\n.jcode/\n' >> .gitignore
  git status --short | grep -c '.jcode'
  ```

  Esperado: `0` — el overlay no debe aparecer como fichero sin seguimiento.

- [ ] **Paso 6: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add .gitignore
  git commit -m "chore: ignore generated jcode vault overlay"
  ```

  `~/.zshrc` está fuera del repo y no se commitea.

---

## Tarea 7: Servidor MCP del vault

Envoltorio fino sobre `vault_lookup.py` y `vault_scout.py`. Lo leen los tres harnesses, y de paso **le devuelve el scout a Codex**, que lo perdió en su port.

**Ficheros:**
- Crear: `tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py`
- Modificar: `tricks_ferrer/graft-mem-vault/self_check_jcode.py`

**Produce:** un servidor MCP stdio con las tools `vault_lookup` y `vault_scout`.

> **Sin dependencias.** El resto de graft-mem-vault es stdlib pura y esto también debe serlo: nada de `mcp` ni `fastmcp`. El protocolo es JSON-RPC línea a línea sobre stdin/stdout, versión `2024-11-05` (la que habla jcode). jcode además **solo admite transporte stdio**.

- [ ] **Paso 1: Escribir la prueba que falla**

  Añade a `self_check_jcode.py`:

  ```python
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
  ```

  Y al runner:

  ```python
      test_mcp_server_handshake_and_tools()
  ```

- [ ] **Paso 2: Ejecutar y verificar que falla**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  ```

  Esperado: FALLA porque `mcp/vault_mcp_server.py` no existe.

- [ ] **Paso 3: Implementar**

  ```bash
  mkdir -p /Users/ferrer/guia/tricks_ferrer/graft-mem-vault/mcp
  ```

  Crea `tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py`:

  ```python
  #!/usr/bin/env python3
  """
  Servidor MCP (stdio, sin dependencias) que expone el vault del proyecto.

  Envuelve vault_lookup.py y vault_scout.py como tools, para que los tres
  harnesses (Claude Code, Codex, jcode) accedan al vault por la misma vía.
  Protocolo JSON-RPC 2.0 línea a línea, versión 2024-11-05.
  """

  import json
  import os
  import subprocess
  import sys

  PROTOCOL = "2024-11-05"
  TIMEOUT = 60

  SCRIPTS = os.environ.get(
      "GRAFT_MEM_VAULT_SCRIPTS", os.path.expanduser("~/.claude/scripts")
  )

  TOOLS = [
      {
          "name": "vault_lookup",
          "description": (
              "Consulta el vault del proyecto por fichero, término, id de "
              "observación o de sesión. Devuelve el índice o la entrada "
              "concreta. Úsalo cuando ya sabes qué fichero o id te interesa."
          ),
          "inputSchema": {
              "type": "object",
              "properties": {
                  "termino": {
                      "type": "string",
                      "description": "Fichero, término, o id a consultar.",
                  }
              },
              "required": ["termino"],
          },
      },
      {
          "name": "vault_scout",
          "description": (
              "Busca en el vault por concepto y devuelve hechos recortados con "
              "su id, en vez de volcar observaciones enteras. Úsalo para "
              "preguntas del tipo «¿ya intentamos X?» o «qué se hizo para Y»."
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


  def send(message):
      sys.stdout.write(json.dumps(message) + "\n")
      sys.stdout.flush()


  def reply(request_id, result):
      send({"jsonrpc": "2.0", "id": request_id, "result": result})


  def run_script(name, argument):
      script = os.path.join(SCRIPTS, name)
      if not os.path.exists(script):
          return f"No encontrado: {script}"
      try:
          proc = subprocess.run(
              [sys.executable, script, argument],
              capture_output=True,
              text=True,
              timeout=TIMEOUT,
          )
      except Exception as error:
          return f"Fallo al ejecutar {name}: {error}"
      return proc.stdout or proc.stderr or "(sin resultados)"


  def call_tool(name, arguments):
      if name == "vault_lookup":
          text = run_script("vault_lookup.py", arguments.get("termino", ""))
      elif name == "vault_scout":
          text = run_script("vault_scout.py", arguments.get("pregunta", ""))
      else:
          text = f"Tool desconocida: {name}"
      return {"content": [{"type": "text", "text": text}]}


  def main():
      for line in sys.stdin:
          line = line.strip()
          if not line:
              continue
          try:
              request = json.loads(line)
          except Exception:
              continue

          method = request.get("method")
          request_id = request.get("id")

          # Las notificaciones no llevan id y no se responden.
          if request_id is None:
              continue

          if method == "initialize":
              reply(request_id, {
                  "protocolVersion": PROTOCOL,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "vault", "version": "1.0.0"},
              })
          elif method == "tools/list":
              reply(request_id, {"tools": TOOLS})
          elif method == "tools/call":
              params = request.get("params") or {}
              reply(request_id, call_tool(
                  params.get("name", ""), params.get("arguments") or {}
              ))
          else:
              send({
                  "jsonrpc": "2.0",
                  "id": request_id,
                  "error": {"code": -32601, "message": f"Método no soportado: {method}"},
              })
      return 0


  if __name__ == "__main__":
      sys.exit(main())
  ```

- [ ] **Paso 4: Ejecutar y verificar que pasa**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 -m py_compile mcp/vault_mcp_server.py
  python3 self_check_jcode.py
  ```

  Esperado: `OK — extracción y composición del overlay`

- [ ] **Paso 5: Probar una llamada real a una tool**

  ```bash
  cd /Users/ferrer/guia
  printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"vault_lookup","arguments":{"termino":"graft-mem-vault"}}}' \
    | timeout 90 python3 tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py \
    | tail -1 | head -c 400
  ```

  Esperado: un JSON con `result.content[0].text` conteniendo salida real del vault.

- [ ] **Paso 6: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py \
          tricks_ferrer/graft-mem-vault/self_check_jcode.py
  git commit -m "feat: add dependency-free MCP server exposing vault lookup and scout"
  ```

---

## Tarea 8: Registrar el MCP y documentar

**Ficheros:**
- Modificar: `.mcp.json`
- Modificar: `tricks_ferrer/graft-mem-vault/README.md`
- Modificar: `resources/README.md`

- [ ] **Paso 1: Registrar el servidor**

  ```bash
  cd /Users/ferrer/guia
  python3 - <<'PY'
  import json, pathlib
  path = pathlib.Path(".mcp.json")
  data = json.loads(path.read_text())
  data.setdefault("mcpServers", {})["vault"] = {
      "command": "python3",
      "args": ["tricks_ferrer/graft-mem-vault/mcp/vault_mcp_server.py"],
  }
  path.write_text(json.dumps(data, indent=2) + "\n")
  print(list(data["mcpServers"]))
  PY
  ```

  Esperado: la lista incluye `graft` y `vault`.

- [ ] **Paso 2: Verificar que jcode lo ve**

  ```bash
  grep -c 'vault' ~/.jcode/logs/*.log 2>/dev/null | tail -1
  ```

  Este paso solo confirma el registro en fichero. La comprobación de verdad la hace el usuario abriendo `jcode` y mirando que la tool aparezca — **pídesela, no la des por hecha**.

- [ ] **Paso 3: Documentar en el README del trick**

  Añade a `tricks_ferrer/graft-mem-vault/README.md` una sección «Instalación en jcode» que cubra: que jcode no inyecta desde hooks y por qué se usa el overlay; el comando `bash install-jcode.sh`; el paso manual del wrapper de `~/.zshrc`; que `vault-read-nudge.py` no está disponible en jcode y por qué; y una fila nueva en la tabla de self-checks para `self_check_jcode.py`.

- [ ] **Paso 4: Actualizar el índice de recursos**

  Añade la mención del servidor MCP del vault en `resources/README.md`, siguiendo el formato de las entradas vecinas.

- [ ] **Paso 5: Ejecutar la batería completa**

  ```bash
  cd /Users/ferrer/guia/tricks_ferrer/graft-mem-vault
  python3 self_check_jcode.py
  python3 self_check_codex.py
  python3 self_check_hooks.py
  ```

  Los tres deben imprimir su línea `OK — …`. Si `self_check_codex.py` o `self_check_hooks.py` fallan, has roto algo existente: PARA y reporta.

- [ ] **Paso 6: Commit**

  ```bash
  cd /Users/ferrer/guia
  git add .mcp.json tricks_ferrer/graft-mem-vault/README.md resources/README.md
  git commit -m "docs: register vault MCP server and document jcode installation"
  ```

---

## Tarea 9: Verificación de extremo a extremo y limpieza

- [ ] **Paso 1: Limpiar los datos de prueba de la Tarea 1**

  Si el paso 6 de la Tarea 1 detectó que las pruebas escribieron en el almacén, borra esas filas por su `session_id` (`parity-probe-1`). Consulta el contrato documentado para saber en qué tabla cayeron. Si no hubo escritura, salta este paso.

- [ ] **Paso 2: Retirar la copia de seguridad si todo está bien**

  ```bash
  ls -la ~/.claude-mem/claude-mem.db ~/.claude-mem/claude-mem.db.bak-parity
  ```

  **No la borres tú.** Enseña ambos tamaños al usuario y pregúntale si quiere conservarla.

- [ ] **Paso 3: Prueba real en jcode**

  Pide al usuario que abra una terminal nueva, entre en `/Users/ferrer/guia`, lance `jcode` y compruebe tres cosas: que el modelo demuestra conocer el contexto del proyecto al arrancar, que la tool `vault_scout` aparece disponible, y que al cerrar la sesión no salta ningún error.

  Esto **no lo puedes verificar tú**: `jcode` necesita un TTY interactivo y no arranca desde una herramienta.

- [ ] **Paso 4: Handoff**

  Crea `thoughts/shared/handoffs/graft-mem-vault-jcode/current.md` recogiendo: qué quedó instalado y dónde, el contrato de `raw` descubierto, qué incógnitas del spec se resolvieron y cuáles siguen abiertas (la carrera de `session_start`, el comportamiento en `/compact`), y que la fase 2 sigue pendiente.

- [ ] **Paso 5: Commit final**

  ```bash
  cd /Users/ferrer/guia
  git add thoughts/shared/handoffs/graft-mem-vault-jcode/current.md
  git commit -m "docs: hand off jcode parity implementation"
  ```
