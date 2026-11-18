# python fetch.py <Go vuln id> <Go versions to backport>
import sys
import requests
import json
from packaging.version import Version

if len(sys.argv) < 3:
    print(json.dumps({"error": "insufficient argument"}, indent=2))
    sys.exit(1)
vuln_id = sys.argv[1]

url = f'https://vuln.go.dev/ID/{vuln_id}.json'
try:
    response = requests.get(url).json()
except requests.exceptions.RequestException as e:
    print(json.dumps({"error": str(e)}, indent=2))
    sys.exit(1)

output = {
    "id": vuln_id,
    "packages": []
}

patch_urls = []
for index in response["references"]:
    ref_url:str = index["url"]
    if ref_url.startswith(("https://go.dev/cl", "https://github.com")):
        patch_urls.append(ref_url)

packages = []
for index in response["affected"]:
    package = index["package"]["name"]
    packages.append(package)

for pkg in packages:
    package = {
        "package": pkg,
        "versions": []
    }
    go_versions = sys.argv[2:]
    go_versions= sorted(go_versions, key=Version, reverse=True)

    resp = requests.get(f'https://proxy.golang.org/{pkg}/@v/list')
    if resp.status_code !=200:
        continue
    tags = resp.text
    for tag in sorted(tags.splitlines(), key=Version, reverse=True):
        try:
            gomodfile = requests.get(f'https://proxy.golang.org/{pkg}/@v/{tag}.mod').text
        except requests.exceptions.RequestException as e:
            print(json.dumps({"error": str(e)}, indent=2))
            sys.exit(1)
        for line in gomodfile.splitlines():
            if line.startswith("go "):
                go_version = line.split()[1]
                for user_go_version in go_versions:
                    if Version(go_version) == Version(user_go_version):
                        package["versions"].append({
                            "go": go_version,
                            "tag": tag
                        })
                        go_versions.remove(user_go_version)
                break
    for version in package["versions"]:
        version["patches"] = []
        for patch_url in patch_urls:
            version["patches"].append({"url": patch_url, "filepath": None, "patch_applied": False})
    output["packages"].append(package)
print(json.dumps(output, indent=2))
