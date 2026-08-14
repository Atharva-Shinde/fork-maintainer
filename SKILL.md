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
| `branch_setup.py` | `<fetch.json path> <tag>` | Checks upstream remote, fetches upstream. If `sustaining-<tag>` exists locally, switches to it and rebases on upstream if available. If only `upstream/sustaining-<tag>` exists, creates local branch from it. Exits on failure if neither exists. |
| `patch.py` | `<fetch.json path> <tag>` | Applies patches only — assumes already on the correct branch. Skips `patch_applied: true`. Updates fetch.json after each successful patch. Exits on failure. Re-runnable after conflict resolution. |
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

Then set `url` to `""` and `filepath` to the resolved absolute path. Note: `setup.py` will skip patches where `filepath` is already set and `url` is empty.

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

## Per-version loop

Read fetch.json. Iterate through `packages[0].versions` in order. For each version:

1. Print:
   ```
   ━━━ PATCHING <tag> (Go <go>) ━━━

   | Patch URL                  | Local File                |
   |----------------------------|---------------------------|
   | <url>                      | <filepath>                |
   ```
2. For the **second version onward**, print the progress table:
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

3. Run steps in order:
   - Step 4 — Branch setup + Patch
   - Step 5 — Verify
   - Step 6 — Report
   - Step 7 — Push
   - Step 8 — Pull Request
4. Move to the next version

---

## Step 4 — Branch setup and patch

### 4a. Branch setup

Run once per version. Do NOT re-run after conflict resolution.

```bash
python3 $HOME/.claude/skills/fork-maintainer/branch_setup.py /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json <tag>
```

On failure, print the error and tell the user what to fix. After user confirms, re-run.

### 4b. Apply patches

```bash
python3 $HOME/.claude/skills/fork-maintainer/patch.py /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json <tag>
```

**Exit code 0:**
```
╔═══════════════════════════════════════════════════════╗
║  ✓ PATCH APPLIED                                      ║
║  Branch: sustaining-<tag>                              ║
╚═══════════════════════════════════════════════════════╝
```
Proceed to Step 5.

**Exit code 1:** Go to conflict resolution below.

---

### Conflict resolution

When `patch.py` fails, print `━━━ CONFLICT: <tag> ━━━` then show the raw outputs:

1. Print the error JSON from `patch.py` — this contains the `git am` stderr
2. `git status` — print full output
3. `git diff` — print full output showing conflict markers
4. `git am --show-current-patch=diff` — print full output showing what the patch tried to apply

Then use `AskUserQuestion`:
- Option 1: `Resolve manually`
- Option 2: `Attempt a fix`
- Option 3: `Use a different patch`

#### Resolve manually

User resolves the conflict on their own — they will run `git add`, `git am --continue`, or apply the patch their way. When user confirms done, update state and proceed to Step 5:
```bash
jq '.packages[0].versions[<VI>].patches[<PI>].patch_applied = true' /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json > /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp && mv /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json
```

#### Attempt a fix

1. Read the conflicted file — look for `<<<<<<<`, `=======`, `>>>>>>>` markers
2. Read the patch hunk from `git am --show-current-patch=diff` — understand what the patch intended to change
3. Using the CVE understanding from Step 3, determine the correct resolution that preserves the fix intent
4. Edit the conflicted file to resolve — remove conflict markers, apply the intended change
5. Show the resolved file to the user for confirmation before proceeding
6. After user confirms: `git add <resolved files>` then `git am --continue`
7. Update state and proceed to Step 5:
```bash
jq '.packages[0].versions[<VI>].patches[<PI>].patch_applied = true' /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json > /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp && mv /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json
```

#### Use a different patch

1. `git am --abort` — reset the failed state
2. Remove the old patch and download the replacement to the same filename:
   ```bash
   PATCH_FILE=<packages[0].versions[VERSION_INDEX].patches[PATCH_INDEX].filepath>
   rm -f "$PATCH_FILE"
   curl -L -o "$PATCH_FILE" "<url>"   # or: cp <local-path> "$PATCH_FILE"
   ```
3. Update fetch.json:
   ```bash
   jq '.packages[0].versions[<VI>].patches[<PI>].url = "<new>" | .packages[0].versions[<VI>].patches[<PI>].patch_applied = false' /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json > /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp && mv /tmp/fork-maintainer/<GO-VULN-ID>/fetch.tmp /tmp/fork-maintainer/<GO-VULN-ID>/fetch.json
   ```
4. Re-run `patch.py` for this tag (do NOT re-run `branch_setup.py`)

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
If Step 3 identified conditional logic, write a small Go file to `/tmp/fork-maintainer/<GO-VULN-ID>/check_gate.go` using the Write tool that prints the gate value, then run:
```bash
GOTOOLCHAIN=go<go-directive> go run /tmp/fork-maintainer/<GO-VULN-ID>/check_gate.go
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
AHEAD=$(git rev-list tags/<base-tag>..HEAD --count)
```

Print:

```
━━━ PUSH: <tag> ━━━

⚠  Review carefully before confirming:

  Remote (origin):    <REMOTE_URL>
  Remote (upstream):  <UPSTREAM_URL>
  Branch:             <BRANCH>
  Commits:            <AHEAD> ahead of <base-tag>
  Pushing to:         origin (<REMOTE_URL>)
```

Use `AskUserQuestion`:
- Option 1: `Yes, push branch` — description: pushes `<BRANCH>` to `<REMOTE_URL>`
- Option 2: `No, skip push` — description: branch stays local, move to next version

If confirmed:

```bash
git push origin <BRANCH>
```

Print result:
- Success: `[<tag>] ✓ pushed to <REMOTE_URL>`
- Failure: `[<tag>] ✗ push failed: <error>` — end turn and ask user how to proceed.

### Step 8 — Pull Request

**8a.** Construct the PR title. The Go version is the `go` directive from the tag's go.mod, not the user's requested version:

```
[Go <go-directive>] <CVE-ID>: <commit subject from patch>
```

Get the commit subject:
```bash
git log -1 --format=%s
```

Get the CVE ID:
```bash
jq -r '.aliases[] | select(startswith("CVE-"))' /tmp/fork-maintainer/<GO-VULN-ID>/vuln.json
```

**8b.** Construct the PR description. List the exact git commands that produced the current state — not what scripts were run, but the underlying git operations. Use actual values from this session:

````
## Process

```bash
git fetch upstream
git switch -c sustaining-<tag> <source>   # or: git switch sustaining-<tag> + git rebase upstream/sustaining-<tag>
git am <patch-filepath-1>
git am <patch-filepath-2>                 # if multiple patches
```

## Conflicts

<if none: "No conflicts — patch applied cleanly.">
<if conflicts occurred, for each file:>
- `<filename>`: <description of conflict and resolution>
  ```bash
  git add <filename>
  git am --continue
  ```

## Patch Source

<for each patch:>
- <patches[].url>

## Verification

```bash
GOTOOLCHAIN=go<go-directive> go vet ./...   # <passed/failed>
```
- Runtime gates: <none or gate=value>

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
