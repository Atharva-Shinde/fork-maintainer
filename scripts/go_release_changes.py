#!/usr/bin/env python3
"""Extract a focused, evidence-linked Go release change catalog as JSON.

The catalog is limited to five kinds of evidence: language features, public API
additions, deprecations, API removals, and behavior-change notes. It does not
classify arbitrary commits or scan maintenance-branch history.

Requires a local golang/go checkout with release tags. Uses the Python standard
library and git. Official release notes are fetched from go.dev only when they
are not present in the tagged checkout.
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
from urllib.error import URLError
from urllib.request import Request, urlopen

ISSUE_RE = re.compile(r"(?:go\.dev/issue/|github\.com/golang/go/issues/|#)(\d{4,})")
API_ENTRY_RE = re.compile(r"^\s*(?:\d+\|\s*)?(pkg\s+.+?)\s*$")
API_FILE_RE = re.compile(r"go1\.(\d+)\.txt$")
PARSER_TYPECHECK_PATHS = (
    "src/go/parser/",
    "src/go/types/",
    "src/cmd/compile/internal/syntax/",
    "src/cmd/compile/internal/types2/",
)
BEHAVIOR_SIGNALS = re.compile(
    r"\b(changed?|changes|now|no longer|instead of|default|behavior|behaviour|"
    r"compatib\w*|panic|recover|error handling|semantics|GODEBUG)\b",
    re.IGNORECASE,
)
DEPRECATION_RE = re.compile(r"\b(deprecat\w*|discouraged|should not be used)\b", re.IGNORECASE)


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def resolve_release_tag(repo: Path, version: str) -> str:
    """Try both initial release tag spellings and return the existing tag."""
    for tag in (f"go{version}.0", f"go{version}"):
        if ref_exists(repo, f"refs/tags/{tag}"):
            return tag
    raise RuntimeError(
        f"No initial release tag found for Go {version}; tried go{version}.0 and go{version}. "
        "Fetch tags with `git fetch --tags origin`."
    )


def git_file(repo: Path, ref: str, path: str) -> str | None:
    if subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode:
        return None
    return run_git(repo, "show", f"{ref}:{path}")


def extract_issue_numbers(text: str) -> list[str]:
    return sorted(set(ISSUE_RE.findall(text)))


class ReleaseNotesParser(HTMLParser):
    """Extract release-note blocks with their nearest heading and source links."""
    BLOCK_TAGS = {"p", "li", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.heading = "Overview"
        self.heading_level = 0
        self.heading_parts: list[str] = []
        self.block_tag: str | None = None
        self.block_depth = 0
        self.block_parts: list[str] = []
        self.block_links: list[str] = []
        self.in_link = False
        self.sections: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_level = int(tag[1])
            self.heading_parts = []
        elif tag in self.BLOCK_TAGS and self.block_tag is None:
            self.block_tag = tag
            self.block_depth = 1
            self.block_parts = []
            self.block_links = []
        elif self.block_tag is not None and tag in self.BLOCK_TAGS:
            self.block_depth += 1
        elif tag == "a" and self.block_tag is not None:
            self.in_link = True
            href = dict(attrs).get("href")
            if href:
                self.block_links.append(href)

    def handle_data(self, data: str) -> None:
        if self.heading_level:
            self.heading_parts.append(data)
        if self.block_tag is not None:
            self.block_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if re.fullmatch(r"h[1-6]", tag) and self.heading_level:
            heading = " ".join("".join(self.heading_parts).split())
            if heading:
                self.heading = heading
            self.heading_level = 0
        elif tag == "a":
            self.in_link = False
        elif self.block_tag is not None and tag in self.BLOCK_TAGS:
            self.block_depth -= 1
            if self.block_depth <= 0:
                text = " ".join("".join(self.block_parts).split())
                if text:
                    self.sections.append({
                        "heading": self.heading,
                        "text": text,
                        "links": list(dict.fromkeys(self.block_links)),
                        "issue_numbers": extract_issue_numbers(
                            text + " " + " ".join(self.block_links)
                        ),
                    })
                self.block_tag = None
                self.block_parts = []
                self.block_links = []


def get_release_notes(repo: Path, tag: str, version: str) -> dict[str, Any]:
    """Read notes from the tag where available, otherwise fetch go.dev notes."""
    candidates = (f"doc/go{version}.html", f"doc/go{version}.md")
    for path in candidates:
        content = git_file(repo, tag, path)
        if content is None:
            continue
        if path.endswith(".html"):
            parser = ReleaseNotesParser()
            parser.feed(content)
            sections = parser.sections
        else:
            heading = "Overview"
            sections = []
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("#"):
                    heading = line.lstrip("# ")
                elif line:
                    sections.append({"heading": heading, "text": line,
                                     "links": [], "issue_numbers": extract_issue_numbers(line)})
        return {
            "source": "repository",
            "path": path,
            "url": f"https://github.com/golang/go/blob/{tag}/{path}",
            "sections": sections,
        }

    url = f"https://go.dev/doc/go{version}"
    try:
        request = Request(url, headers={"User-Agent": "go-release-change-extractor/1.0"})
        with urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8", errors="replace")
        parser = ReleaseNotesParser()
        parser.feed(content)
        return {"source": "go.dev", "path": None, "url": url,
                "sections": parser.sections}
    except (OSError, URLError) as exc:
        return {"source": "unavailable", "path": None, "url": url,
                "sections": [], "error": str(exc)}


def parse_api_file(text: str | None) -> set[str]:
    """Parse API inventory entries; line-number prefixes are optional."""
    if not text:
        return set()
    entries = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = API_ENTRY_RE.match(line)
        if match:
            entries.add(match.group(1).strip())
    return entries


def api_files(repo: Path, tag: str, through_minor: int) -> dict[int, set[str]]:
    """Read cumulative version inventories from api/go1.N.txt files."""
    found: dict[int, set[str]] = {}
    for minor in range(1, through_minor + 1):
        path = f"api/go1.{minor}.txt"
        content = git_file(repo, tag, path)
        if content is not None:
            found[minor] = parse_api_file(content)
    return found


def cumulative_api(inventories: dict[int, set[str]], through_minor: int) -> set[str]:
    return set().union(*(entries for minor, entries in inventories.items()
                         if minor <= through_minor)) if inventories else set()


def api_change_records(repo: Path, base_tag: str, release_tag: str,
                       previous_minor: int, target_minor: int,
                       previous_version: str, version: str) -> list[dict[str, Any]]:
    previous_inventory = api_files(repo, base_tag, previous_minor)
    target_inventory = api_files(repo, release_tag, target_minor)
    previous_api = cumulative_api(previous_inventory, previous_minor)
    target_api = cumulative_api(target_inventory, target_minor)
    target_release_entries = target_inventory.get(target_minor, set())

    records: list[dict[str, Any]] = []
    old_url = f"https://github.com/golang/go/blob/{base_tag}/api/go1.{previous_minor}.txt"
    new_url = f"https://github.com/golang/go/blob/{release_tag}/api/go1.{target_minor}.txt"

    for declaration in sorted(target_release_entries - previous_api):
        if "//deprecated" in declaration.lower():
            category = "deprecation"
            status = "evidence_extracted"
        else:
            category = "public_api_addition"
            status = "evidence_extracted"
        records.append({
            "category": category,
            "title": declaration,
            "summary": declaration,
            "status": status,
            "confidence": "high",
            "evidence": [{"type": "api_inventory", "url": new_url}],
            "issue_numbers": extract_issue_numbers(declaration),
        })

    # API inventories are cumulative by release file; compare their unions, not
    # just go1.N.txt against go1.(N-1).txt, to detect actual missing entries.
    for declaration in sorted(previous_api - target_api):
        records.append({
            "category": "api_removal_candidate",
            "title": declaration,
            "summary": declaration,
            "status": "review_required",
            "confidence": "candidate",
            "evidence": [
                {"type": "api_inventory", "url": old_url},
                {"type": "api_inventory", "url": new_url},
            ],
            "issue_numbers": extract_issue_numbers(declaration),
        })

    # Deprecation markers can apply to APIs introduced in earlier releases.
    # Emit only deprecations recorded in this target release's inventory above;
    # release-note deprecation notices are collected separately.
    return records


def parser_typechecker_commits(repo: Path, base_tag: str,
                               release_tag: str) -> list[dict[str, Any]]:
    """Find commits touching Go parser/type-checker implementation paths."""
    args = ["log", "--format=%H%x1f%s%x1f%b", f"{base_tag}..{release_tag}", "--"]
    args.extend(PARSER_TYPECHECK_PATHS)
    raw = run_git(repo, *args)
    records = []
    # Each commit record is separated by a blank line; file paths are not
    # requested, keeping this command output unambiguous.
    for row in raw.split("\n\n"):
        fields = row.split("\x1f", 2)
        if len(fields) != 3:
            continue
        sha, subject, body = fields
        full = subject + "\n" + body
        records.append({"commit": sha.strip(), "subject": subject.strip(),
                        "issue_numbers": extract_issue_numbers(full),
                        "url": f"https://github.com/golang/go/commit/{sha.strip()}"})
    return records


def note_evidence(note: dict[str, Any], notes_url: str) -> dict[str, Any]:
    return {"type": "release_note", "heading": note["heading"],
            "url": notes_url, "excerpt": note["text"],
            "links": note.get("links", [])}


def release_note_changes(notes: dict[str, Any], parser_commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract behavior and deprecation notes; language requires parser evidence."""
    changes: list[dict[str, Any]] = []
    notes_url = notes["url"]
    for note in notes["sections"]:
        heading = note["heading"].strip()
        heading_lower = heading.lower()
        text = note["text"].strip()
        issue_ids = set(note.get("issue_numbers", []))
        evidence = [note_evidence(note, notes_url)]

        if DEPRECATION_RE.search(text):
            changes.append({
                "category": "deprecation",
                "title": heading,
                "summary": text,
                "status": "review_required",
                "confidence": "candidate",
                "evidence": evidence,
                "issue_numbers": sorted(issue_ids),
            })

        # A language feature is included only when a release-note item is
        # accompanied by a parser/type-checker commit tied to the same issue.
        if heading_lower == "language" or heading_lower.startswith("language "):
            matched = [commit for commit in parser_commits
                       if issue_ids.intersection(commit["issue_numbers"])]
            if matched:
                changes.append({
                    "category": "language_feature",
                    "title": heading,
                    "summary": text,
                    "status": "evidence_linked",
                    "confidence": "high",
                    "evidence": evidence + [
                        {"type": "parser_typechecker_commit", "url": c["url"],
                         "commit": c["commit"], "subject": c["subject"]}
                        for c in matched
                    ],
                    "issue_numbers": sorted(issue_ids),
                })

        # Restrict behavior candidates to release-note sections explicitly
        # describing compatibility or text that signals a behavior change.
        if ("compatib" in heading_lower or "behavior" in heading_lower
                or "behaviour" in heading_lower or BEHAVIOR_SIGNALS.search(text)):
            changes.append({
                "category": "behavior_change_candidate",
                "title": heading,
                "summary": text,
                "status": "review_required",
                "confidence": "candidate",
                "evidence": evidence,
                "issue_numbers": sorted(issue_ids),
            })
    return changes


def unique_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate repeated API declarations and release-note records."""
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for change in changes:
        key = (change["category"], change["title"], change["summary"])
        if key not in seen:
            seen.add(key)
            unique.append(change)
    for index, change in enumerate(unique, 1):
        change["id"] = f"change-{index:04d}"
    return unique


def parse_version(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?:go)?(\d+)\.(\d+)", value)
    if not match:
        raise argparse.ArgumentTypeError("version must look like 1.21 or go1.21")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="local golang/go checkout")
    parser.add_argument("--version", required=True, type=parse_version, help="target minor version, e.g. 1.21")
    parser.add_argument("--output", type=Path, help="write JSON here; stdout if omitted")
    args = parser.parse_args()

    repo = args.repo.resolve()
    major, target_minor = args.version
    if major != 1 or target_minor < 2:
        parser.error("this focused extractor supports Go 1.x releases from 1.2 onward")
    version = f"{major}.{target_minor}"
    previous_minor = target_minor - 1
    previous_version = f"{major}.{previous_minor}"

    try:
        run_git(repo, "rev-parse", "--git-dir")
        base_tag = resolve_release_tag(repo, previous_version)
        release_tag = resolve_release_tag(repo, version)
        notes = get_release_notes(repo, release_tag, version)
        parser_commits = parser_typechecker_commits(repo, base_tag, release_tag)
        changes = api_change_records(
            repo, base_tag, release_tag, previous_minor, target_minor,
            previous_version, version,
        )
        changes.extend(release_note_changes(notes, parser_commits))
        changes = unique_changes(changes)

        counts: dict[str, int] = {}
        for change in changes:
            counts[change["category"]] = counts.get(change["category"], 0) + 1

        output = {
            "schema_version": 3,
            "language": "Go",
            "version": version,
            "previous_version": previous_version,
            "base_tag": base_tag,
            "release_tag": release_tag,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scope": [
                "language_feature",
                "public_api_addition",
                "deprecation",
                "api_removal_candidate",
                "behavior_change_candidate",
            ],
            "sources": {
                "base_tag": f"https://github.com/golang/go/tree/{base_tag}",
                "release_tag": f"https://github.com/golang/go/tree/{release_tag}",
                "api_inventory": f"https://github.com/golang/go/tree/{release_tag}/api",
                "release_notes": notes["url"],
            },
            "release_notes": notes,
            "changes": changes,
            "summary": {"total": len(changes), "by_category": counts},
            "methodology": {
                "api_additions": "Entries in api/go1.X.txt absent from the previous cumulative API inventory.",
                "api_removals": "Difference between cumulative API inventories; reported as candidates for review.",
                "deprecations": "API //deprecated markers in the target release inventory and explicit release-note notices.",
                "language_features": "Release-note Language entries are emitted only when a linked issue matches a parser/type-checker implementation commit.",
                "behavior_changes": "Release-note entries with compatibility/behavior headings or behavior-change signals; candidates require review.",
                "limitations": [
                    "Release-note classification is conservative and may omit items without recognizable headings or issue links.",
                    "API removal candidates require manual verification against Go compatibility policy and api/except.txt.",
                    "Behavior-change candidates are not automatically asserted to be breaking.",
                    "This output excludes general commit history and maintenance-branch patch changes.",
                ],
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
