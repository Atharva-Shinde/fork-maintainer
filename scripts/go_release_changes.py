#!/usr/bin/env python3
"""Extract evidence-backed Go release change candidates into JSON.

Requires a local golang/go checkout with release tags and the target release
branch fetched. Uses only Python's standard library and git.
"""
from __future__ import annotations

import argparse
import datetime as dt
from html.parser import HTMLParser
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

API_LINE = re.compile(r"^\s*\d+\|\s?(.*?)\s*$")
ISSUE = re.compile(r"(?<![\w/])#(\d{4,})(?!\d)")


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def resolve_release_tag(repo: Path, version: str) -> str:
    """Resolve an initial release tag, trying both Go tag spellings."""
    candidates = (f"go{version}.0", f"go{version}")
    for candidate in candidates:
        if ref_exists(repo, f"refs/tags/{candidate}"):
            return candidate
    tried = ", ".join(candidates)
    raise RuntimeError(
        f"cannot find initial release tag for Go {version}; tried {tried}. "
        "Fetch tags with `git fetch --tags origin`."
    )


def exists(repo: Path, revision: str, path: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{revision}:{path}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def show_file(repo: Path, revision: str, path: str) -> str | None:
    if not exists(repo, revision, path):
        return None
    return git(repo, "show", f"{revision}:{path}")


def parse_api(text: str | None) -> set[str]:
    if not text:
        return set()
    result = set()
    for line in text.splitlines():
        match = API_LINE.match(line)
        if match and match.group(1).strip():
            result.add(match.group(1).strip())
    return result


class NotesParser(HTMLParser):
    """Keep release-note text grouped by HTML headings."""
    BLOCKS = {"p", "li", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.heading_level = 0
        self.current_heading = "Overview"
        self.heading_text: list[str] = []
        self.block_depth = 0
        self.block_text: list[str] = []
        self.sections: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_level = int(tag[1])
            self.heading_text = []
        elif tag in self.BLOCKS:
            self.block_depth += 1

    def handle_data(self, data: str) -> None:
        if self.heading_level:
            self.heading_text.append(data)
        if self.block_depth:
            self.block_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if re.fullmatch(r"h[1-6]", tag) and self.heading_level:
            title = " ".join("".join(self.heading_text).split())
            if title:
                self.current_heading = title
            self.heading_level = 0
        elif tag in self.BLOCKS and self.block_depth:
            self.block_depth -= 1
            text = " ".join("".join(self.block_text).split())
            if text:
                self.sections.append({"heading": self.current_heading, "text": text})
            self.block_text = []


def release_notes(repo: Path, tag: str, version: str) -> dict[str, Any]:
    candidates = [f"doc/go{version}.html", f"doc/go{version}.md"]
    for path in candidates:
        text = show_file(repo, tag, path)
        if text is not None:
            if path.endswith(".html"):
                parser = NotesParser()
                parser.feed(text)
                sections = parser.sections
            else:
                heading = "Overview"
                sections = []
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        heading = stripped.lstrip("# ")
                    elif stripped:
                        sections.append({"heading": heading, "text": stripped})
            return {
                "path": path,
                "url": f"https://github.com/golang/go/blob/{tag}/{path}",
                "sections": sections,
            }
    return {"path": None, "url": f"https://go.dev/doc/go{version}", "sections": []}


def tag_key(tag: str) -> tuple[int, ...]:
    match = re.fullmatch(r"go(\d+)\.(\d+)(?:\.(\d+))?", tag)
    if not match:
        return (0,)
    return tuple(int(part or 0) for part in match.groups())


def available_tags(repo: Path, version: str, branch: str) -> list[str]:
    pattern = re.compile(rf"go{re.escape(version)}(?:\.\d+)?$")
    tags = [t for t in git(repo, "tag", "--list", f"go{version}*").splitlines()
            if pattern.fullmatch(t)]
    reachable = []
    for tag in sorted(tags, key=lambda value: (tag_key(value), value)):
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", tag, branch],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            reachable.append(tag)
    return reachable


def commit_records(repo: Path, start: str, end: str, version: str,
                   stage: str) -> list[dict[str, Any]]:
    fmt = "%H%x1f%aI%x1f%an%x1f%s"
    raw = git(repo, "log", "--reverse", f"--format={fmt}", f"{start}..{end}")
    records = []
    for row in raw.splitlines():
        parts = row.split("\x1f", 3)
        if len(parts) != 4:
            continue
        sha, authored, author, subject = parts
        body = git(repo, "show", "-s", "--format=%B", sha)
        files = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        lower = (subject + "\n" + body).lower()
        labels = []
        if re.search(r"\b(cve-\d{4}-\d+|security|vulnerability|security fix)\b", lower):
            labels.append("security_candidate")
        if re.search(r"\b(deprecat\w*|remove[sd]?|breaking|incompatib\w*|behavior change)\b", lower):
            labels.append("compatibility_review_candidate")
        if re.search(r"\b(add|implement|support|introduc\w*)\b", lower):
            labels.append("feature_candidate")
        if not labels:
            labels.append("general_change")
        issue_ids = sorted(set(ISSUE.findall(body)))
        records.append({
            "category": "patch_history" if stage.startswith("patch") else "release_history",
            "stage": stage,
            "version": f"{version}" if stage.startswith("release") else stage.removeprefix("patch:"),
            "commit": sha,
            "date": authored,
            "author": author,
            "subject": subject,
            "candidate_labels": labels,
            "issue_numbers": issue_ids,
            "files": files,
            "body": body,
            "sources": [f"https://github.com/golang/go/commit/{sha}"],
        })
    return records


def api_changes(repo: Path, previous: str, target: str, previous_version: str,
                version: str) -> list[dict[str, Any]]:
    old = parse_api(show_file(repo, previous, f"api/go{previous_version}.txt"))
    new = parse_api(show_file(repo, target, f"api/go{version}.txt"))
    changes = []
    for category, entries in (("api_added", sorted(new - old)),
                              ("api_removed_candidate", sorted(old - new))):
        for declaration in entries:
            deprecated = "//deprecated" in declaration.lower()
            actual_category = "api_deprecated" if deprecated else category
            ids = ISSUE.findall(declaration)
            changes.append({
                "category": actual_category,
                "declaration": declaration,
                "issue_numbers": sorted(set(ids)),
                "confidence": "high" if deprecated or category == "api_added" else "review_required",
                "note": "API inventory diff; removal candidates require manual verification." if category == "api_removed_candidate" else None,
                "sources": [
                    f"https://github.com/golang/go/blob/{previous}/api/go{previous_version}.txt",
                    f"https://github.com/golang/go/blob/{target}/api/go{version}.txt",
                ],
            })
    for declaration in sorted(new):
        if "//deprecated" in declaration.lower() and declaration in old:
            changes.append({
                "category": "api_deprecated", "declaration": declaration,
                "issue_numbers": sorted(set(ISSUE.findall(declaration))),
                "confidence": "high",
                "sources": [f"https://github.com/golang/go/blob/{target}/api/go{version}.txt"],
            })
    return changes


def build_changes(version: str, release_tag: str, api_entries: list[dict[str, Any]],
                  release_history: list[dict[str, Any]],
                  patch_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a stable, normalized change catalog from authoritative and candidate evidence."""
    normalized: list[dict[str, Any]] = []

    for entry in api_entries:
        category = entry["category"]
        if category == "api_added":
            normalized_category = "public_api_addition"
            title = "Public API added"
        elif category == "api_deprecated":
            normalized_category = "deprecation"
            title = "Public API deprecated"
        else:
            normalized_category = "potential_api_removal"
            title = "Public API removal candidate"

        normalized.append({
            "id": f"api-{len(normalized) + 1:04d}",
            "category": normalized_category,
            "title": title,
            "summary": entry["declaration"],
            "migration": [],
            "severity": "unknown" if category == "api_removed_candidate" else "low",
            "confidence": entry["confidence"],
            "status": "review_required" if category == "api_removed_candidate" else "evidence_extracted",
            "affected_packages": [],
            "affected_code_patterns": [],
            "issue_numbers": entry["issue_numbers"],
            "sources": entry["sources"],
        })

    for record in release_history:
        labels = record["candidate_labels"]
        if "general_change" in labels and len(labels) == 1:
            continue

        category = "security_candidate" if "security_candidate" in labels else \
                   "compatibility_review_candidate" if "compatibility_review_candidate" in labels else \
                   "feature_candidate"
        normalized.append({
            "id": f"commit-{record['commit'][:12]}",
            "category": category,
            "title": record["subject"],
            "summary": "",
            "migration": [],
            "severity": "unknown",
            "confidence": "candidate",
            "status": "needs_review",
            "affected_packages": [],
            "affected_code_patterns": [],
            "issue_numbers": record["issue_numbers"],
            "sources": record["sources"],
            "provenance": {
                "stage": record["stage"],
                "commit": record["commit"],
                "files": record["files"],
            },
        })

    for record in patch_history:
        labels = record["candidate_labels"]
        if "security_candidate" in labels:
            category = "security_patch_candidate"
        elif "compatibility_review_candidate" in labels:
            category = "compatibility_patch_candidate"
        elif "feature_candidate" in labels:
            category = "feature_patch_candidate"
        else:
            continue

        normalized.append({
            "id": f"commit-{record['commit'][:12]}",
            "category": category,
            "title": record["subject"],
            "summary": "",
            "migration": [],
            "severity": "unknown",
            "confidence": "candidate",
            "status": "needs_review",
            "affected_packages": [],
            "affected_code_patterns": [],
            "issue_numbers": record["issue_numbers"],
            "sources": record["sources"],
            "provenance": {
                "stage": record["stage"],
                "commit": record["commit"],
                "files": record["files"],
            },
        })

    return normalized


def parse_version(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?:go)?(\d+)\.(\d+)", value)
    if not match:
        raise argparse.ArgumentTypeError("version must look like 1.21 or go1.21")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="local golang/go git checkout")
    parser.add_argument("--version", required=True, type=parse_version, help="target minor version, e.g. 1.21")
    parser.add_argument("--branch", help="maintenance branch (default: release-branch.goX.Y)")
    parser.add_argument("--output", type=Path, help="write JSON here; stdout if omitted")
    args = parser.parse_args()

    repo = args.repo.resolve()
    major, minor = args.version
    version = f"{major}.{minor}"
    branch = args.branch or f"release-branch.go{version}"
    previous_version = f"{major}.{minor - 1}"

    try:
        git(repo, "rev-parse", "--git-dir")
        base_tag = resolve_release_tag(repo, previous_version)
        release_tag = resolve_release_tag(repo, version)

        if branch.startswith("origin/"):
            branch_ref = branch
            branch_name = branch.removeprefix("origin/")
        else:
            branch_ref = branch
            branch_name = branch

        if not ref_exists(repo, branch_ref):
            raise RuntimeError(
                f"cannot resolve branch/ref {branch_ref!r}; fetch it or pass the local ref name with --branch"
            )

        patch_tags = available_tags(repo, version, branch_ref)
        if release_tag not in patch_tags:
            raise RuntimeError(f"{release_tag} is not reachable from {branch_ref}; fetch tags/branch first")

        changes = api_changes(repo, base_tag, release_tag, previous_version, version)
        notes = release_notes(repo, release_tag, version)
        history = commit_records(repo, base_tag, release_tag, version, "release")

        previous = release_tag
        patch_history = []
        for tag in patch_tags:
            if tag_key(tag) <= tag_key(release_tag):
                continue
            patch_history.extend(commit_records(repo, previous, tag, version, f"patch:{tag}"))
            previous = tag
        if git(repo, "rev-parse", branch_ref) != git(repo, "rev-parse", previous):
            patch_history.extend(commit_records(repo, previous, branch_ref, version, "patch:branch-tip"))

        normalized_changes = build_changes(version, release_tag, changes, history, patch_history)

        output = {
            "schema_version": 2,
            "language": "Go",
            "version": version,
            "previous_version": previous_version,
            "base_tag": base_tag,
            "release_tag": release_tag,
            "maintenance_branch": branch_name,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "methodology": {
                "release_range": f"{base_tag}..{release_tag}",
                "patch_ranges": "successive reachable goX.Y[.Z] tags, then latest tag to branch tip",
                "limitations": [
                    "Commit labels are heuristic candidates, not authoritative classifications.",
                    "API inventory does not capture language or behavioral changes; review release notes and commits.",
                    "An API removal candidate can be caused by inventory format or baseline differences; verify manually.",
                    "Patch branch history may include backports and should not be treated as new minor-version features.",
                    "The changes array separates extracted facts from needs-review candidates, but does not replace human curation.",
                ],
            },
            "release_notes": notes,
            "changes": normalized_changes,
            "api_changes": changes,
            "release_commits": history,
            "patch_commits": patch_history,
            "summary": {
                "normalized_changes": len(normalized_changes),
                "api_added": sum(c["category"] == "api_added" for c in changes),
                "api_deprecated": sum(c["category"] == "api_deprecated" for c in changes),
                "api_removed_candidates": sum(c["category"] == "api_removed_candidate" for c in changes),
                "release_commits": len(history),
                "patch_commits": len(patch_history),
                "release_note_sections": len(notes["sections"]),
            },
            "sources": {
                "base_tag": f"https://github.com/golang/go/tree/{base_tag}",
                "release_tag": f"https://github.com/golang/go/tree/{release_tag}",
                "maintenance_branch": f"https://github.com/golang/go/tree/{branch_name}",
                "release_notes": notes["url"],
            },
        }
        serialized = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        else:
            sys.stdout.write(serialized)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
