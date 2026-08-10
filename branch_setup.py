# python branch_setup.py <fetch.json> <tag>
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

tag = version["tag"]

# check if upstream exists
check_upstream = subprocess.run(("git", "remote", "get-url", "upstream"), capture_output=True, text=True)
if check_upstream.returncode == 0:
        print(json.dumps({"info": check_upstream.stdout}))
        fetch_upstream = subprocess.run(("git", "fetch", "upstream"), capture_output=True, text=True)
        if fetch_upstream.returncode == 0:
            print(json.dumps({"info": fetch_upstream.stdout}))
        else:
            print(json.dumps({"error": f'could not fetch upstream, {fetch_upstream.stderr}'}))
            sys.exit(1)
else:
    print(json.dumps({"error": f'could not find upstream, {check_upstream.stderr}'}))
    sys.exit(1)

# does upstream have the sustaining branch?
check_remote = subprocess.run(("git", "branch", "-r", "--list", f'upstream/sustaining-{tag}'), capture_output=True, text=True)
# does local have the sustaining branch?
check_local = subprocess.run(("git", "branch", "--list", f'sustaining-{tag}'), capture_output=True, text=True)

if check_local.stdout.strip():
    # local exists — switch to it
    switch_branch = subprocess.run(("git", "switch", f'sustaining-{tag}'), capture_output=True, text=True)
    if switch_branch.returncode == 0:
        print(json.dumps({"info": switch_branch.stdout}))
    else:
        print(json.dumps({"error": f'could not switch to sustaining-{tag}, {switch_branch.stderr}'}))
        sys.exit(1)
    # rebase on upstream if available
    if check_remote.stdout.strip():
        rebase_upstream = subprocess.run(("git", "rebase", f'upstream/sustaining-{tag}'), capture_output=True, text=True)
        if rebase_upstream.returncode == 0:
            print(json.dumps({"info": rebase_upstream.stdout}))
        else:
            print(json.dumps({"error": f'failed to rebase on upstream, {rebase_upstream.stderr}'}))
            sys.exit(1)
else:
    # local doesn't exist
    if check_remote.stdout.strip():
        # create from upstream
        create_branch = subprocess.run(("git", "switch", "-c", f'sustaining-{tag}', f'upstream/sustaining-{tag}'), capture_output=True, text=True)
        if create_branch.returncode == 0:
            print(json.dumps({"info": create_branch.stdout}))
        else:
            print(json.dumps({"error": f'could not create sustaining-{tag}, {create_branch.stderr}'}))
            sys.exit(1)
    else:
        # no upstream, no local — cannot proceed
        print(json.dumps({"error": f'upstream does not have sustaining-{tag} branch'}))
        sys.exit(1)
