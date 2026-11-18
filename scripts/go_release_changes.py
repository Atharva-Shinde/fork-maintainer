#!/usr/bin/env python3
"""Extract evidence-backed Go release change candidates into JSON.

Requires a local golang/go checkout with release tags and the target release
branch fetched. Uses only Python's standard library and git.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
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
    prefix = f"go{version}."
    tags = [t for t in git(repo, "tag", "--list", f"{prefix}*").splitlines()
            if re.fullmatch(re.escape(prefix) + r"\d+", t)]
    reachable = []
    for tag in sorted(tags, key=tag_key):
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
    # A deprecation may remain in both inventories and therefore not appear in the diff.
    for declaration in sorted(new):
        if "//deprecated" in declaration.lower() and declaration in old:
            changes.append({
                "category": "api_deprecated", "declaration": declaration,
                "issue_numbers": sorted(set(ISSUE.findall(declaration))),
                "confidence": "high",
                "sources": [f"https://github.com/golang/go/blob/{target}/api/go{version}.txt"],
            })
    return changes


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
    base_tag, release_tag = f"go{previous_version}.0", f"go{version}.0"

    try:
        git(repo, "rev-parse", "--git-dir")
        for ref in (base_tag, release_tag, branch):
            git(repo, "rev-parse", "--verify", ref)
        patch_tags = available_tags(repo, version, branch)
        if release_tag not in patch_tags:
            raise RuntimeError(f"{release_tag} is not reachable from {branch}; fetch tags/branch first")

        changes = api_changes(repo, base_tag, release_tag, previous_version, version)
        notes = release_notes(repo, release_tag, version)
        history = commit_records(repo, base_tag, release_tag, version, "release")

        # Attribute maintenance commits to each successive patch tag; include any branch-tip delta.
        previous = release_tag
        patch_history = []
        for tag in patch_tags:
            if tag_key(tag) <= tag_key(release_tag):
                continue
            patch_history.extend(commit_records(repo, previous, tag, version, f"patch:{tag}"))
            previous = tag
        if git(repo, "rev-parse", branch) != git(repo, "rev-parse", previous):
            patch_history.extend(commit_records(repo, previous, branch, version, "patch:branch-tip"))

        output = {
            "schema_version": 1,
            "language": "Go",
            "version": version,
            "previous_version": previous_version,
            "release_tag": release_tag,
            "maintenance_branch": branch,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "methodology": {
                "release_range": f"{base_tag}..{release_tag}",
                "patch_ranges": "successive reachable goX.Y.Z tags, then latest tag to branch tip",
                "limitations": [
                    "Commit labels are heuristic candidates, not authoritative classifications.",
                    "API inventory does not capture language or behavioral changes; review release notes and commits.",
                    "An API removal candidate can be caused by inventory format or baseline differences; verify manually.",
                    "Patch branch history may include backports and should not be treated as new minor-version features.",
                ],
            },
            "release_notes": notes,
            "api_changes": changes,
            "release_commits": history,
            "patch_commits": patch_history,
            "summary": {
                "api_added": sum(c["category"] == "api_added" for c in changes),
                "api_deprecated": sum(c["category"] == "api_deprecated" for c in changes),
                "api_removed_candidates": sum(c["category"] == "api_removed_candidate" for c in changes),
                "release_commits": len(history),
                "patch_commits": len(patch_history),
                "release_note_sections": len(notes["sections"]),
            },
            "sources": {
                "release_tag": f"https://github.com/golang/go/tree/{release_tag}",
                "maintenance_branch": f"https://github.com/golang/go/tree/{branch}",
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
