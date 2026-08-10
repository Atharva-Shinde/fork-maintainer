# fork-maintainer

A skill to create fork patches for CVE remediation.

## Prerequisites

- `git`
- `python3` with `requests`, `packaging`, and `PyGithub`
```bash
pip install requests packaging PyGithub
```
- `jq`
- `go`
- `GITHUB_TOKEN` env var (for PR creation only)
```bash
export GITHUB_TOKEN=<your-token>
```
## Install

```bash
git clone https://github.com/Atharva-Shinde/fork-maintainer.git
cd fork-maintainer
chmod +x install.sh
./install.sh
```

## Usage

Clone the affected module's sustaining fork to your personal GitHub account, then:

```bash
cd <cloned-module>
claude
/fork-maintainer <GO-VULN-ID> <go-versions...>
```

Example:

```
/fork-maintainer GO-2026-5026 1.24 1.23
```

The skill will walk you through each step, prompting for confirmation at key points.

## What it does

Given a Go vulnerability ID and target Go versions, the skill:

1. Fetches CVE details and finds compatible upstream tags
2. Downloads patch files from the Go vulnerability database
3. Analyzes the CVE, its exploit, and the fix
4. Applies patches to sustaining branches (`sustaining-<tag>`)
5. Verifies backport completeness, compatibility, and runtime gates
6. Generates per-version reports
7. Pushes branches to the user's fork
8. Creates PRs on the upstream sustaining repo

## How it works

The skill combines deterministic scripts with AI orchestration:

| Script | Purpose |
|--------|---------|
| `fetch.py` | Queries the Go vulnerability database and module proxy to find compatible tags |
| `setup.py` | Verifies go.mod and downloads patch files |
| `branch_setup.py` | Creates or switches to `sustaining-<tag>` branch, syncs with upstream |
| `patch.py` | Applies patches via `git am`, re-runnable after conflict resolution |
| `pullrequest.py` | Creates PRs on the upstream sustaining repo |

The skill (SKILL.md) orchestrates these scripts, handles conflicts with AI-assisted resolution, verifies backports, and manages user interaction.

## Files

```
fork-maintainer/
├── fetch.py            # CVE lookup + version selection
├── setup.py            # Environment check + patch download
├── branch_setup.py     # Branch creation + upstream sync
├── patch.py            # Patch application (re-runnable)
├── pullrequest.py      # PR creation on upstream repo
├── SKILL.md            # Skill definition (Claude Code reads this)
├── install.sh          # Copies files to ~/.claude/skills/fork-maintainer/
└── README.md
```
