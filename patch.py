# python patch.py <fetch.json> <tag>
import sys
import json
import subprocess

if len(sys.argv) < 3:
    print(json.dumps({"error": "insufficient argument"}, indent=2))
    sys.exit(1)

filepath = sys.argv[1]
with open(filepath, "r") as f:
    data = json.load(f)

t = sys.argv[2]
index = None
for i, v in enumerate(data["packages"][0]["versions"]):
    if v["tag"] == t:
        index = i
        break
if index is None:
    print(json.dumps({"error": f'tag {t} not found in data'}))
    sys.exit(1)

version = data["packages"][0]["versions"][index]

# TODO: check if we are on the required branch

# apply the patches
for patch in version["patches"]:
    if patch["patch_applied"]:
        continue
    apply_patch = subprocess.run(("git", "am", patch["filepath"]), capture_output=True, text=True)
    if apply_patch.returncode == 0:
        print(json.dumps({"info": apply_patch.stdout}))
        patch["patch_applied"] = True
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    else:
        print(json.dumps({"error": f'could not apply the patch, {apply_patch.stderr}'}))
        sys.exit(1)
