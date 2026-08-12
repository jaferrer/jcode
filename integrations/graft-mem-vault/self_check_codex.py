#!/usr/bin/env python3
"""Contract checks for Codex: vault adapter and native claude-mem hooks."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOOK = ROOT / "hooks" / "vault-read-nudge.py"
INSTALL = ROOT / "install-codex.sh"


def test_codex_path_extractors():
    namespace = runpy.run_path(str(HOOK))
    extraer_ruta = namespace["extraer_ruta"]

    assert extraer_ruta(
        {
            "tool_name": "mcp__codebase_memory_mcp__get_code_snippet",
            "tool_response": {
                "structuredContent": {"file_path": "/tmp/proyecto/src/foo.py"}
            },
        }
    ) == "/tmp/proyecto/src/foo.py"
    assert extraer_ruta(
        {
            "tool_name": "apply_patch",
            "tool_input": {"command": "*** Update File: src/foo.py\n"},
        }
    ) == "src/foo.py"
    assert extraer_ruta({"tool_name": "Bash", "tool_input": {"command": "pwd"}}) is None


def test_post_tool_use_does_not_return_mcp_output_override():
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        scripts = home / "scripts"
        scripts.mkdir()
        note = home / "note.md"
        note.write_text("- [[obs-1]] — Hallazgo previo\n")
        (scripts / "vault_lookup.py").write_text(
            "def resolver_vault(*args): return 'PRUEBA', '/tmp/vault'\n"
            f"def buscar(*args): return [('nota', {str(note)!r})]\n"
            "def seccion(texto, nombre): return texto\n"
        )
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__codebase_memory_mcp__get_code_snippet",
            "tool_response": {
                "structuredContent": {"file_path": str(home / "example.py")}
            },
        }
        environment = os.environ.copy()
        environment["GRAFT_MEM_VAULT_HOME"] = str(home)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            cwd=home,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output.get("systemMessage")
        assert "hookSpecificOutput" not in output, output


def test_codex_install_finds_codex_plugin_cache():
    with tempfile.TemporaryDirectory() as temporary:
        codex_home = Path(temporary) / "codex"
        claude_home = Path(temporary) / "claude-not-installed"
        plugin_hooks = (
            codex_home
            / "plugins/cache/claude-mem-local/claude-mem/13.14.0/hooks/codex-hooks.json"
        )
        plugin_hooks.parent.mkdir(parents=True)
        plugin_root = plugin_hooks.parent.parent
        (plugin_root / "scripts").mkdir()
        (plugin_root / "scripts/bun-runner.js").touch()
        (plugin_root / "scripts/worker-service.cjs").touch()
        plugin_hooks.write_text(json.dumps({"hooks": {}}))
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "CLAUDE_MEM_DB": os.path.expanduser("~/.claude-mem/claude-mem.db"),
            }
        )
        result = subprocess.run(
            ["bash", str(INSTALL), "--check"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_codex_install_is_idempotent():
    with tempfile.TemporaryDirectory() as temporary:
        codex_home = Path(temporary)
        claude_home = codex_home / "claude"
        plugin_hooks = (
            claude_home
            / "plugins/cache/thedotmack/claude-mem/13.12.4/hooks/codex-hooks.json"
        )
        plugin_hooks.parent.mkdir(parents=True)
        plugin_root = plugin_hooks.parent.parent
        (plugin_root / "scripts").mkdir()
        (plugin_root / "scripts/bun-runner.js").touch()
        (plugin_root / "scripts/worker-service.cjs").touch()
        native_events = {
            "SessionStart": ("startup|resume", "context"),
            "UserPromptSubmit": (None, "session-init"),
            "PreToolUse": ("^Bash$", "file-context"),
            "PostToolUse": (".*", "observation"),
            "Stop": (None, "summarize"),
        }
        plugin_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {
                                **({"matcher": matcher} if matcher else {}),
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            "CLAUDE_MEM_CODEX_HOOK=1 node worker-service.cjs "
                                            f"hook codex {handler}"
                                        ),
                                    }
                                ],
                            }
                        ]
                        for event, (matcher, handler) in native_events.items()
                    }
                }
            )
        )
        (codex_home / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "existing"}]},
                            {
                                "matcher": "startup|resume",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            "CLAUDE_MEM_CODEX_HOOK=1 old-worker "
                                            "hook codex context"
                                        ),
                                    }
                                ],
                            },
                        ]
                    }
                }
            )
        )
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "CLAUDE_MEM_DB": os.path.expanduser("~/.claude-mem/claude-mem.db"),
            }
        )

        for _ in range(2):
            result = subprocess.run(
                ["bash", str(INSTALL)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr

        configuration = json.loads((codex_home / "hooks.json").read_text())
        commands = []
        for event, groups in configuration["hooks"].items():
            for group in groups:
                for hook in group.get("hooks", []):
                    if "graft-mem-vault" in hook.get("command", ""):
                        commands.append((event, hook["command"]))

        assert len(commands) == 4, commands
        assert any(
            hook.get("command") == "existing"
            for group in configuration["hooks"]["SessionStart"]
            for hook in group.get("hooks", [])
        )
        assert not any(
            "old-worker" in hook.get("command", "")
            for groups in configuration["hooks"].values()
            for group in groups
            for hook in group.get("hooks", [])
        )
        for event, (_, handler) in native_events.items():
            groups = [
                group
                for group in configuration["hooks"].get(event, [])
                if any(
                    "CLAUDE_MEM_CODEX_HOOK=1" in hook.get("command", "")
                    and " hook codex " in hook.get("command", "")
                    for hook in group.get("hooks", [])
                )
            ]
            assert len(groups) == 1, (event, groups)
            assert any(
                f"hook codex {handler}" in hook.get("command", "")
                for hook in groups[0]["hooks"]
            )
        native_session_start = [
            group
            for group in configuration["hooks"]["SessionStart"]
            if any(
                "CLAUDE_MEM_CODEX_HOOK=1" in hook.get("command", "")
                for hook in group.get("hooks", [])
            )
        ][0]
        assert native_session_start["matcher"] == "startup|resume|compact|clear"
        context_command = native_session_start["hooks"][0]["command"]
        assert str(plugin_root / "scripts/bun-runner.js") in context_command
        assert "version-check.js" not in context_command
        assert (codex_home / "graft-mem-vault" / "scripts" / "vault_lookup.py").exists()


if __name__ == "__main__":
    test_codex_path_extractors()
    test_post_tool_use_does_not_return_mcp_output_override()
    test_codex_install_finds_codex_plugin_cache()
    test_codex_install_is_idempotent()
    print("OK — extractores Codex e instalación idempotente")
