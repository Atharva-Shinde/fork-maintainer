#python pullrequest.py <branch> <title> <description>
import os
from github import Github
from github import GithubException
import subprocess
import json
import sys

if len(sys.argv) < 4:
    print(json.dumps({"error": "insufficient argument"}, indent=2))
    sys.exit(1)

branch = sys.argv[1]
title = sys.argv[2]
description = sys.argv[3]

token = os.environ.get("GITHUB_TOKEN")
if not token:
    print(json.dumps({"error": "GITHUB_TOKEN environment variable is not set"}))
    sys.exit(1)

g = Github(token)

username = g.get_user().login

raw_upstream = subprocess.run(("git", "remote", "get-url", "upstream"), capture_output=True, text=True)
if raw_upstream.returncode == 0:
    print(json.dumps({"info": raw_upstream.stdout}))
else:
    print(json.dumps({"error": f'failed to get the upstream reference, {raw_upstream.stderr}'}))
    sys.exit(1)
raw_upstream = raw_upstream.stdout
org_repo = raw_upstream.strip().removesuffix(".git").split("github.com")[-1].lstrip(":/")

repo = g.get_repo(org_repo)
try:
    repo.get_branch(branch=branch)
except GithubException as e:
    if e.status == 404:
        print(json.dumps({"error": f'upstream target branch does not exist, ask maintainers to create a {branch} branch'}))
        sys.exit(1)
    else:
        print(json.dumps({"error": f'an unexpected error occured, {str(e)}'}))
        sys.exit(1)
try:
    pull_request = repo.create_pull(base=branch, head=f'{username}:{branch}', title=title, body=description)
    print(json.dumps({"info": pull_request.html_url}))
except GithubException as e:
    print(json.dumps({"error": f'an unexpected error occured, {str(e)}'}))
    sys.exit(1)
