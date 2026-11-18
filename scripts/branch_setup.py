# python branch_setup.py <fetch.json> <tag>
import sys
import json
import subprocess

if len(sys.argv) < 3:
    print(json.dumps({"error": "insufficient argument"}))
    sys.exit(1)

filepath = sys.argv[1]
with open(filepath, "r") as f:
    data = json.load(f)

tag = sys.argv[2]
if not any(v["tag"] == tag for v in data["packages"][0]["versions"]):
    print(json.dumps({"error": f"tag {tag} not found in data"}))
    sys.exit(1)

branch = f"sustaining-{tag}"

# verify upstream remote and fetch
result = subprocess.run(("git", "remote", "get-url", "upstream"), capture_output=True, text=True)
if result.returncode != 0:
    print(json.dumps({"error": f"could not find upstream, {result.stderr.strip()}"}))
    sys.exit(1)

result = subprocess.run(("git", "fetch", "upstream"), capture_output=True, text=True)
if result.returncode != 0:
    print(json.dumps({"error": f"could not fetch upstream, {result.stderr.strip()}"}))
    sys.exit(1)

has_remote = subprocess.run(
    ("git", "branch", "-r", "--list", f"upstream/{branch}"),
    capture_output=True, text=True
).stdout.strip()

has_local = subprocess.run(
    ("git", "branch", "--list", branch),
    capture_output=True, text=True
).stdout.strip()

if has_local:
    result = subprocess.run(("git", "switch", branch), capture_output=True, text=True)
    if result.returncode != 0:
        print(json.dumps({"error": f"could not switch to {branch}, {result.stderr.strip()}"}))
        sys.exit(1)
    if has_remote:
        result = subprocess.run(("git", "rebase", f"upstream/{branch}"), capture_output=True, text=True)
        if result.returncode != 0:
            print(json.dumps({"error": f"failed to rebase on upstream, {result.stderr.strip()}"}))
            sys.exit(1)
elif has_remote:
    result = subprocess.run(
        ("git", "switch", "-c", branch, f"upstream/{branch}"),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(json.dumps({"error": f"could not create {branch}, {result.stderr.strip()}"}))
        sys.exit(1)
else:
    print(json.dumps({"error": f"upstream does not have {branch} branch"}))
    sys.exit(1)

print(json.dumps({"info": f"on branch {branch}"}))
