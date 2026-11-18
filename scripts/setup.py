# python setup.py <fetch.json>
import requests
import sys
import json
import os


if len(sys.argv) < 2:
    print(json.dumps({"error": "insufficient argument"}, indent=2))
    sys.exit(1)

filepath = sys.argv[1]
with open(filepath, "r") as f:
    data = json.load(f)

os.makedirs(f'/tmp/fork-maintainer/{data["id"]}', exist_ok=True)

# check the environment
try:
    with open("go.mod", "r") as f:
        for line in f:
            if line.startswith("module "):
                if not line.split()[1] == data["packages"][0]["package"]:
                    print(json.dumps({"error": f'environment expected {data["packages"][0]["package"]} but got {line.split()[1]}'}))
                    sys.exit(1)
except FileNotFoundError:
    print(json.dumps({"error": "go.mod not found"}))
    sys.exit(1)

# download the patches
for version in data["packages"][0]["versions"]:
    for i, patch in enumerate(version["patches"]):
        patch_filepath = f'/tmp/fork-maintainer/{data["id"]}/{version["tag"]}-{data["id"]}-{i}.patch'
        try:
            response = requests.get(patch["url"]).text
        except requests.exceptions.RequestException as e:
            print(json.dumps({"error": str(e)}, indent=2))
            sys.exit(1)
        with open(patch_filepath, "w") as f:
            f.write(response)
        print(json.dumps({"info": f'patch file downloaded at {patch_filepath}'}))
        patch["filepath"] = patch_filepath

# append fetch.json
with open(filepath, "w") as f:
    json.dump(data, f, indent=2)