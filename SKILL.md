---
name: fork-maintainer
description: |
  Create OpenShift Sustaining fork patches for CVE remediation — contributor workflow.
  Deterministic steps are scripted; the skill orchestrates.
  Triggers on: '/fork-maintainer'.
---

# fork-maintainer

Create a sustaining fork patch for a Go CVE. Contributor workflow only.

## Invocation

```
/fork-maintainer <GO-VULN-ID> <go-versions...>
```

Example: `/fork-maintainer GO-2026-5026 1.22 1.23`

If Go versions are not provided in the invocation, ask for them before running Step 1.

## Architecture

Scripts live at `$HOME/.claude/skills/fork-maintainer/` (installed via `install.sh`). The skill runs them, parses the JSON with `jq`, and orchestrates the workflow. Never reimplement what a script already does.

| Script | Input | Output |
|--------|-------|--------|
| `fetch.py` | `<GO-VULN-ID> <go-versions...>` | JSON: top-level `id` and `packages` array. Each package has `package` (module name) and `versions` array. Each version has `go`, `tag`, and `patches` array. Each patch has `url`, `filepath` (null), `patch_applied` (false). |
| `setup.py` | `<fetch.json path>` | Creates `/tmp/fork-maintainer/<id>/`, verifies `go.mod`, downloads patch files, sets `filepath` on each version's patches, writes back into fetch.json. |
| `patch.py` | `<fetch.json path> <tag>` | Patches a single version: creates `sustaining-<tag>` branch, or if it exists, switches to it and rebases on `upstream/sustaining-<tag>`. Applies each patch via `git am`. After each successful patch, fetches upstream tags and creates `<tag>-sec.<N>` (auto-increments). Skips `patch_applied: true`. Exits on failure. |
| `pullrequest.py` | `<branch> <title> <description>` | Creates a PR on the upstream repo. Requires `upstream` remote. Verifies target branch exists before creating. Exits on failure. |

## Rules

- When prompting the user, print the question in plain text and **end your turn**. Do not use `AskUserQuestion` unless explicitly noted.
- No filler, no narration. Never print "Now moving to Step X" or "Proceeding to Step X" — the `━━━ TITLE ━━━` headers are the step markers. Run commands, print results, wait for input when needed, move on.
- Never fabricate URLs, commit SHAs, or data not in script/API output.
- If any script errors out, print the exact error output. If the error is user-resolvable (missing remote, auth failure, missing dependency, wrong directory), tell the user what to fix, end turn, and wait. When the user confirms it's resolved, re-run the failed step. Never retry or work around errors silently.
- Never run `go build`. It is not part of any step.
- Never read, print, log, or reference secrets, tokens, API keys, or credentials. Scripts handle their own authentication — the skill must have zero awareness of sensitive values.
- When writing JSON to fetch.json, use `jq` only. Never use `cat <<EOF`, `echo`, `python3 -c`, or the Write tool.

## Formatting

- **Section headers:** `━━━ TITLE ━━━` (use `━` U+2501 box-drawing heavy horizontal)
- **Success:** `✓` prefix
- **Failure:** `✗` prefix
- **Info:** `→` prefix
- **Warning:** `⚠` prefix
- **Tables:** Use markdown tables with alignment. Never truncate URLs — show them in full even if the table is wide.
- **Key-value blocks:** Align values with padding:
  ```
  CVE:         CVE-2026-39821
  Module:      golang.org/x/net
  Impact:      DoS
  ```

---

## Step 0 — Setup working directory

```bash
rm -rf /tmp/fork-maintainer/<GO-VULN-ID>
mkdir -p /tmp/fork-maintainer/<GO-VULN-ID>
```

All intermediate files for this Go Vulnerability go here. Scripts read from and write to this directory.

---

## Step 1 — Fetch CVE details and version tags

The user provides the Go Vuln ID and Go versions in the invocation args. Pass them all to `fetch.py`:

```bash
python3 $HOME/.claude/skills/fork-maintainer/fetch.py <GO-VULN-ID> <go-versions...> | tee /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json | jq '.'
```

Print the parsed output in this format:

```
━━━ FETCH RESULTS ━━━

Package: <package name>

| Go     | Tag       | Patches                    |
|--------|-----------|----------------------------|
| <go>   | <tag>     | <url>, <url>               |
```

Each version has its own set of patch URLs. The user can provide different patches per version.

Then use `AskUserQuestion` with:
- Option 1: `Yes, proceed` — description: confirms the packages, tags, and patch URLs are correct
- Option 2: `No, provide corrections` — user can change tags, add/remove/replace patch URLs for specific versions, or provide local patch file paths instead of URLs

If the user selects "No, provide corrections", end turn and let the user describe what to change. Corrections can be per-version — e.g. "use a different patch for v0.43.0" or "add this local patch for v0.35.0". Apply corrections and rewrite fetch.json using `jq`.

If the user provides a local file path instead of a URL, resolve it to an absolute path first:

```bash
realpath <user-provided-path>
```

Then set `url` to `""` and `filepath` to the resolved absolute path. This tells `setup.py` to skip downloading that patch (filepath is already set).

Print the updated table and re-ask. Only this one question until "Yes, proceed".

Proceed to Step 2.

---

## Step 2 — Setup

```bash
python3 $HOME/.claude/skills/fork-maintainer/setup.py /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json
```

Verifies `go.mod` matches the expected package and downloads all patches. Print results as:

```
━━━ SETUP ━━━

→ go.mod verified: <package name>
✓ Downloaded: <filepath>
✓ Downloaded: <filepath>
```

On error (wrong module, missing `go.mod`, download failure), print the error with `✗` prefix and ask the user how to proceed.

---

## Step 3 — Understand the CVE and its fix

This step runs for the **first version** before the loop starts. For subsequent versions, it only re-runs if that version's patch URLs differ from the ones already analyzed.

### 3a. Gather sources

```bash
curl -s "https://vuln.go.dev/ID/<GO-VULN-ID>.json" -o /tmp/fork-maintainer/<GO-VULN-ID>/vuln.json
```

Extract the CVE ID for use in later steps (report filenames, PR titles):

```bash
jq -r '.aliases[] | select(startswith("CVE-"))' /tmp/fork-maintainer/<GO-VULN-ID>/vuln.json
```

Read the patch files from the first version's `patches[].filepath` in fetch.json.

### 3b. Understand the vulnerability

Read the vuln DB `details` field (not just `summary`). Then read the **source code of the affected symbols at the first version's tag** using `git show tags/<tag>:<file>` (not the working tree — the branch hasn't been created yet) to understand:

- What do the affected functions do?
- What input do they accept?
- Where is the boundary between trusted and untrusted input?
- What goes wrong — is it missing validation, incorrect parsing, unchecked bounds, wrong assumptions?

### 3c. Understand the exploit

From the vuln details and the affected code, trace:

- How does an attacker trigger this? What input or sequence of actions?
- What is the impact — DoS, privilege escalation, data leak, RCE?
- Is it reachable from external input or only internal callers?

### 3d. Understand the fix

Read the patch diff line by line:

- What is the root cause the patch addresses?
- How does the fix prevent the exploit — new validation, bounds check, type assertion, error return?
- Read any new tests added by the patch — they show exactly how the vulnerability is triggered and how the fix prevents it
- Identify which changes are the actual fix vs incidental (formatting, refactoring, unrelated). Note: "the commit may contain changes not strictly related to the CVE fix"

### 3e. Summarize

Print:

```
━━━ CVE ANALYSIS ━━━

CVE:            <CVE ID>
Attack vector:  <how it is exploited — specific input/conditions>
Impact:         <what happens — DoS, escalation, etc.>
Root cause:     <what is wrong in the code>
Affected code:  <symbols/functions and what they do>
Fix approach:   <what the patch changes and why it prevents the exploit>
Scoped to CVE:  <yes / no — flag unrelated changes in the patch>
New tests:      <what the patch tests validate>
```

This context is carried forward to Steps 4 and 5.

**Re-run rule:** Inside the loop, before patching each version, compare that version's `patches[].url` values against the ones already analyzed. If they differ, re-run Steps 3a–3e using that version's tag and patches. If they match, skip — the existing analysis applies.

---

## Steps 4 & 5 — Patch and verify each version

Read fetch.json. Iterate through `packages[0].versions` in order. For each version where any `patches[].patch_applied` is `false`:

1. Get the `tag` and `go` version from that entry
2. Print:
   ```
   ━━━ PATCHING <tag> (Go <go>) ━━━

   | Patch URL                  | Local File                |
   |----------------------------|---------------------------|
   | <url>                      | <filepath>                |
   ```
3. For the **second version onward**, print the status table and patches before asking:

   ```
   ━━━ PROGRESS ━━━

   | Go    | Tag       | Status     |
   |-------|-----------|------------|
   | <go>  | <tag>     | ✓ done     |
   | <go>  | <tag>     | → next     |
   | <go>  | <tag>     |   pending  |
   ```
   Then use `AskUserQuestion`:
   - Option 1: `Yes, proceed` — use these patches
   - Option 2: `No, use different patches` — user provides alternatives
   The first version was already confirmed in Step 1 — proceed directly.
- Run [Step 4 — Patch](#step-4--patch) for that tag
- Run [Step 5 — Verify](#step-5--verify) for that tag
- Run [Step 6 — Report](#step-6--generate-report) for that tag
- Run [Step 7 — Push](#step-7--push) for that tag
- Run [Step 8 — Pull Request](#step-8--pull-request) for that tag
- Move to the next version

### Step 4 — Patch

```bash
python3 $HOME/.claude/skills/fork-maintainer/patch.py /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json <tag>
```

**Exit code 0:**
```
╔═══════════════════════════════════════════════════════╗
║  ✓ PATCH APPLIED                                      ║
║  Branch: sustaining-<tag>    Tag: <tag>-sec.<N>        ║
╚═══════════════════════════════════════════════════════╝
```

**Exit code 1 — error is user-resolvable:** Print the error, tell the user what to fix, end turn. After user confirms, re-run `patch.py` for the same tag.

**Exit code 1 — `git am` conflict:** Print `━━━ CONFLICT: <tag> ━━━` then:

1. `git status` — show conflicted files
2. `git diff` — show conflict markers
3. Read the failed patch hunk from `patches[].filepath`
4. Ask: `Resolve manually, should I attempt a fix, or use a different patch?` — end turn.

**If user resolves manually:** wait for confirmation, then `git add <files>` and `git am --continue`.

**If user asks Claude to attempt:** read conflict markers, read patch hunk, apply fix, show result for confirmation.

**If user provides a different patch:**
```bash
git am --abort
PATCH_FILE=<packages[0].versions[VERSION_INDEX].patches[PATCH_INDEX].filepath>
rm -f "$PATCH_FILE"
curl -L -o "$PATCH_FILE" "<url>"   # or: cp <local-path> "$PATCH_FILE"
jq '.packages[0].versions[<VI>].patches[<PI>].url = "<new>" | .packages[0].versions[<VI>].patches[<PI>].patch_applied = false' /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json > /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp && mv /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json
```

**After any resolution**, re-run `patch.py` for the same tag:
```bash
python3 $HOME/.claude/skills/fork-maintainer/patch.py /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json <tag>
```

If conflict was resolved manually, update state first:
```bash
jq '.packages[0].versions[<VI>].patches[<PI>].patch_applied = true' /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json > /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp && mv /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json
```

### Step 5 — Verify

After all patches for this tag are applied, print `━━━ VERIFY: <tag> ━━━` and verify. **Every claim must come from command output — never assume.**

Use `GOTOOLCHAIN=go<go-directive>` for all commands so verification runs at the target Go version, not the locally installed one.

**5a. Diff completeness:**
```bash
git diff --stat tags/<tag>..HEAD
```
Print output. For each file the original patch modifies (from Step 3), check it appears in the diff.

**5b. Compatibility:**
```bash
GOTOOLCHAIN=go<go-directive> go vet ./...
```
Print full output. Confirms imports, references, types are valid at the target Go version.

**5c. Runtime gates:**
If Step 3 identified conditional logic, run code to check the actual value:
```bash
cd /tmp/fork-maintainer/<GO-VULN-ID>
cat > check_gate.go << 'EOF'
package main
import "fmt"
// import what's needed to evaluate the gate
func main() {
    // print the gate value
}
EOF
GOTOOLCHAIN=go<go-directive> go run check_gate.go
```
Print output. If the gate makes the fix inactive, use `AskUserQuestion` with:
- Option 1: `Use a different patch for this version` — user provides an alternative that doesn't have the gate
- Option 2: `Continue as-is` — accept the fix is inactive on this version

**5d. Self-check:**
For every claim — what command output proves it? If none, say `UNVERIFIED`.

Present results with evidence:
```
✓ Diff complete: <n> files changed
✓ go vet: passed
✓ Runtime gates: none / ⚠ <gate> = <value>
```

Then continue to next version.

After all versions are patched and verified, print:

```
━━━ COMPLETE ━━━

| Go     | Tag       | Status     |
|--------|-----------|------------|
| <go>   | <tag>     | ✓ patched  |
| <go>   | <tag>     | ✓ patched  |

All versions patched. Reports at /tmp/fork-maintainer/<GO-VULN-ID>/
```

### Step 6 — Generate report

Write the report for this version to `/tmp/fork-maintainer/<GO-VULN-ID>/<CVE-ID>-<tag>-report.md`. Use the actual commands, outputs, and values from this session.

```markdown
# <GO-VULN-ID> — <tag> (Go <go-directive>)

## CVE
- **ID:** <CVE ID>
- **Module:** <module>
- **Summary:** <from Step 3>
- **Attack vector:** <from Step 3>
- **Impact:** <from Step 3>
- **Root cause:** <from Step 3>

## Backport target
- **Tag:** <tag>
- **Go directive:** <go version from tag's go.mod>
- **Branch:** sustaining-<tag>

## Patches
- For each patch in this version's `patches` array:
  - **URL:** <url>
  - **File:** <filepath>
  - **Applied:** <patch_applied>

## Patch application
- **Branch created:** sustaining-<tag> from tags/<tag>
- **Patches applied:** <list each patch — clean or conflict>
- **Conflicts:** <none, or list files and how resolved>

## Verification
- **Completeness:** `git diff --stat` output — <all expected files present / missing files listed>
- **Compatibility:** `go vet` output — <passed / failed with errors>
- **Runtime gates:** `check_gate.go` output — <none / gate name = value, fix active or inactive>

## Commands run
<exact commands from this session for this version, in order>
```

Print the report path after writing.

### Step 7 — Push

This step pushes the patched branch and tag to the user's remote. **This is irreversible — be explicit about every detail before executing.**

First, gather and display all push details:

```bash
REMOTE_URL=$(git remote get-url origin)
UPSTREAM_URL=$(git remote get-url upstream 2>/dev/null || echo "not set")
BRANCH=$(git branch --show-current)
TAG=$(git describe --tags --exact-match HEAD 2>/dev/null)
AHEAD=$(git rev-list tags/<base-tag>..HEAD --count)
```

Print:

```
━━━ PUSH: <tag> ━━━

⚠  Review carefully before confirming:

  Remote (origin):    <REMOTE_URL>
  Remote (upstream):  <UPSTREAM_URL>
  Branch:             <BRANCH>
  Tag:                <TAG>
  Commits:            <AHEAD> ahead of <base-tag>
  Pushing to:         origin (<REMOTE_URL>)
```

Use `AskUserQuestion`:
- Option 1: `Yes, push branch and tag` — description: pushes `<BRANCH>` and `<TAG>` to `<REMOTE_URL>`
- Option 2: `No, skip push` — description: branch stays local, move to next version

If confirmed:

```bash
git push origin <BRANCH> <TAG>
```

Print result:
- Success: `[<tag>] ✓ pushed to <REMOTE_URL>`
- Failure: `[<tag>] ✗ push failed: <error>` — end turn and ask user how to proceed.

### Step 8 — Pull Request

**8a.** Construct the PR title. The Go version is the `go` directive from the tag's go.mod, not the user's requested version:

```
[Go <go-directive>, sustaining-<tag>] <CVE-ID>: <commit subject from patch>
```

Get the commit subject:
```bash
git log -1 --format=%s
```

Get the CVE ID:
```bash
jq -r '.aliases[] | select(startswith("CVE-"))' /tmp/fork-maintainer/<GO-VULN-ID>/vuln.json
```

**8b.** Construct the PR description. Use the exact commands and values from this session:

````
## Process

```bash
git switch -c sustaining-<tag> tags/<tag>
git am <patch filepath>
# <for each patch file applied, list the exact git am command>
```

## Conflicts

<if none: "No conflicts — patch applied cleanly.">
<if conflicts occurred, for each file:>
- `<filename>`: <what the conflict was and how it was resolved>

## Patch Source

<for each patch:>
- <patches[].url>

## Verification

- `go vet ./...`: <passed/failed>
- Runtime gates: <none or gate=value>

## Tag

`<tag>-sec.<N>`
````

**8c.** Print the PR details for user review:

```
━━━ PULL REQUEST: <tag> ━━━

| Field           | Value                              |
|-----------------|------------------------------------|
| Title           | <constructed title>                |
| Upstream repo   | <org/repo from upstream remote>    |
| Local repo      | <org/repo from origin remote>      |
| Target branch   | sustaining-<tag>                   |
| Head branch     | <username>:sustaining-<tag>        |

Description:
<print the full PR description constructed in 8b>
```

Then use `AskUserQuestion`:
- Option 1: `Yes, create PR` — creates the PR
- Option 2: `No, skip PR` — move to next version

**8d.** If confirmed, run:

```bash
python3 $HOME/.claude/skills/fork-maintainer/pullrequest.py "sustaining-<tag>" "<title>" "<description>"
```

Parse the JSON output. On success, `pullrequest.py` returns `{"info": "<PR URL>"}`. Print using Unicode box-drawing characters. Measure the URL length first and size the box to fit — the URL must not truncate or overflow:

```
╔═══════════════════════════════════════════════════════════════════╗
║  ✓ PR CREATED                                                     ║
║                                                                   ║
║  https://github.com/openshift-sustaining/<repo>/pull/<number>       ║
╚═══════════════════════════════════════════════════════════════════╝
```

The URL line must be a clickable markdown link: `[<URL>](<URL>)` placed inside the box. Pad the box width to the URL length + 4 characters of margin.

On failure: `✗ PR failed: <error>` — end turn and ask user how to proceed.
