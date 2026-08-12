import json, os, sys

config_path = sys.argv[1]
models_json = os.environ["OMNIROUTE_MODELS_JSON"]

try:
    data = json.loads(models_json)
    # owned_by=="combo" also includes OmniRoute's 38 built-in generic
    # auto/* routers (shipped with every install, not user-configured).
    # Only the slash-free ids are the named combos shown in the hub menu.
    combos = sorted(
        m["id"] for m in data.get("data", [])
        if m.get("owned_by") == "combo" and m.get("id") and "/" not in m["id"]
    )
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

current_ids = sorted(
    lines[i].split("=", 1)[1].strip().strip('"')
    for i in range(start, end)
    if lines[i].strip().startswith("id =")
)

if current_ids == combos:
    sys.exit(0)

block = "".join(f'[[providers.omniroute.models]]\nid = "{c}"\n\n' for c in combos)
new_lines = lines[:start] + [block] + lines[end:]

with open(config_path, "w") as f:
    f.writelines(new_lines)

print(f"synced {len(combos)} omniroute combos", file=sys.stderr)
