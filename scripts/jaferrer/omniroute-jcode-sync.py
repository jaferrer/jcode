import json, os, sys

config_path = sys.argv[1]
models_json = os.environ["OMNIROUTE_MODELS_JSON"]


def toml_str(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def context_window(model):
    """OmniRoute's advertised window for a combo.

    Combos inherit the minimum context of their member models, exposed as
    `context_length` (with `max_input_tokens` as a synonym). Without it jcode
    falls back to DEFAULT_CONTEXT_LIMIT (200K) because it cannot classify
    user-named combo ids, silently truncating 1M-capable routes.
    """
    for key in ("context_length", "max_input_tokens"):
        value = model.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def combo_order(combo):
    """Keep the picker grouped by the upstream provider behind each combo."""
    name = combo[0].lower()
    if name.startswith("copilot-"):
        group = 0
    elif name.endswith("-wa") or name.startswith(("fable", "opus", "sonnet")):
        group = 1
    elif name.startswith("kimi"):
        group = 2
    elif name.startswith("qoder-"):
        group = 3
    else:
        group = 4
    return group, name


try:
    data = json.loads(models_json)
    # owned_by=="combo" also includes OmniRoute's 38 built-in generic
    # auto/* routers (shipped with every install, not user-configured).
    # Only the slash-free ids are the named combos shown in the hub menu.
    combos = sorted(
        (m["id"], context_window(m))
        for m in data.get("data", [])
        if m.get("owned_by") == "combo" and m.get("id") and "/" not in m["id"]
    )
    combos.sort(key=combo_order)
except Exception:
    sys.exit(0)

if not combos:
    sys.exit(0)

with open(config_path) as f:
    lines = f.readlines()

start = None
for i, l in enumerate(lines):
    if l.strip() == "[[providers.omniroute.models]]":
        for j in range(i, -1, -1):
            if lines[j].startswith("[providers."):
                if lines[j].strip() == "[providers.omniroute]":
                    start = i
                break
        if start is not None:
            break

if start is None:
    sys.exit(0)

end = len(lines)
for i in range(start + 1, len(lines)):
    if lines[i].startswith("[") and lines[i].strip() != "[[providers.omniroute.models]]":
        end = i
        break

current = []
for i in range(start, end):
    stripped = lines[i].strip()
    if stripped.startswith("id ="):
        current.append([json.loads(stripped.split("=", 1)[1].strip()), None])
    elif stripped.startswith("context_window =") and current:
        try:
            current[-1][1] = int(stripped.split("=", 1)[1].strip())
        except ValueError:
            pass

if [tuple(entry) for entry in current] == combos:
    sys.exit(0)

block = ""
for combo_id, window in combos:
    block += f"[[providers.omniroute.models]]\nid = {toml_str(combo_id)}\n"
    if window:
        block += f"context_window = {window}\n"
    block += "\n"

new_lines = lines[:start] + [block] + lines[end:]

with open(config_path, "w") as f:
    f.writelines(new_lines)

print(f"synced {len(combos)} omniroute combos", file=sys.stderr)
