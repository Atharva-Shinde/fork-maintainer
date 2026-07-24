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
tag = version["tag"]

# check if upstream exists
check_upstream = subprocess.run(("git", "remote", "get-url", "upstream"), capture_output=True, text=True)
if check_upstream.returncode == 0:
        print(json.dumps({"info": check_upstream.stdout}))
else:
    print(json.dumps({"error": f'could not find upstream, {check_upstream.stderr}'}))
    sys.exit(1)

# switch to sustaining-<tag> branch if it already exists
check_branch = subprocess.run(("git", "branch", "--list", f'sustaining-{tag}'), capture_output=True, text=True)
if check_branch.stdout.strip():
    switch_branch = subprocess.run(("git", "switch", f'sustaining-{tag}'), capture_output=True, text=True)
    if switch_branch.returncode == 0:
        print(json.dumps({"info": switch_branch.stdout}))
    else:
        print(json.dumps({"error": f'could not create/switch to a new branch, {switch_branch.stderr}'}))
        sys.exit(1)
    fetch_upstream = subprocess.run(("git", "fetch", "upstream", f'sustaining-{tag}'), capture_output=True, text=True)
    if fetch_upstream.returncode == 0:
        print(json.dumps({"info": fetch_upstream.stdout}))
    else:
        print(json.dumps({"error": f'failed to fetch the upstream sustaining-{tag} branch, {fetch_upstream.stderr}'}))
        sys.exit(1)
    rebase_upstream = subprocess.run(("git", "rebase", f'upstream/sustaining-{tag}'), capture_output=True, text=True)
    if rebase_upstream.returncode == 0:
        print(json.dumps({"info": rebase_upstream.stdout}))
    else:
        print(json.dumps({"error": f'failed to rebase, {rebase_upstream.stderr}'}))
        sys.exit(1)
else:
    # create a sustaining-<tag> branch
    create_branch = subprocess.run(("git", "switch", "-c", f'sustaining-{tag}', f'tags/{tag}'), capture_output=True, text=True)
    if create_branch.returncode == 0:
        print(json.dumps({"info": create_branch.stdout}))
    else:
        print(json.dumps({"error": f'could not create/switch to a new branch, {create_branch.stderr}'}))
        sys.exit(1)

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
        fetch_tags = subprocess.run(("git", "fetch", "upstream", "--tags"), capture_output=True, text=True)
        if fetch_tags.returncode == 0:
            print(json.dumps({"info": fetch_tags.stdout}))
        else:
            print(json.dumps({"error": f'failed to fetch upstream tags, {fetch_tags.stderr}'}))
            sys.exit(1)
        list_tags = subprocess.run(("git", "tag", "--list", f'{tag}-sec.*'), capture_output=True, text=True)
        if list_tags.returncode == 0:
            print(json.dumps({"info": list_tags.stdout}))
        else:
            print(json.dumps({"error": f'failed to list upstream tags, {list_tags.stderr}'}))
            sys.exit(1)
        max_patch_number = 1
        if not list_tags.stdout == "":
            for tg in list_tags.stdout.split():
                patch_number = tg.split(f'{tag}-sec.')
                if int(patch_number[1]) > max_patch_number:
                    max_patch_number = int(patch_number[1])
            max_patch_number = max_patch_number+1
        create_tag = subprocess.run(("git", "tag", "-a", f'{tag}-sec.{max_patch_number}', "-m", f'Fix {data["id"]} in {version["go"]}'), capture_output=True, text=True)
        if create_tag.returncode == 0:
            print(json.dumps({"info": create_tag.stdout}))
        else:
            print(json.dumps({"error": f'failed to create the {tag}-sec.{max_patch_number} tag, {create_tag.stderr}'}))
            sys.exit(1)

    else:
        print(json.dumps({"error": f'could not apply the patch, {apply_patch.stderr}'}))
        sys.exit(1)
