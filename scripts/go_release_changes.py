#!/usr/bin/env python3
"""Extract evidence-linked Go release changes as JSON.

The catalog covers language features, public API additions, deprecations, API
removal candidates, and documented behavior changes. It compares tagged Go API
inventories and parses the official release-note Markdown from golang/website.
Related implementation commits and test files are attached only when linked by
an issue ID. Maintenance-branch commits are excluded.

Requires Python 3 and git. The Go checkout needs the previous and target release
tags. Release notes are read from a website checkout when supplied, otherwise
fetched from the official golang/website repository.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
API_PACKAGE_RE = re.compile(r"^pkg\s+([^,\s]+)")
ISSUE_SUFFIX_RE = re.compile(r"\s+#\d{4,}\s*$")
PLATFORM_RE = re.compile(r"\s+\([^)]+(?:-[^)]+)*\)(?=,)")
PARSER_TYPECHECK_PATHS = (
    "src/go/parser/", "src/go/types/",
    "src/cmd/compile/internal/syntax/", "src/cmd/compile/internal/types2/",
)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
HEADING_ID_RE = re.compile(r"\s*\{#[^}]+\}\s*$")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$")

# These signals describe observable behavior, rather than API additions or
# implementation/performance improvements.
BEHAVIOR_SIGNALS = re.compile(
    r"\b(no longer|previously|instead of|now returns?|now requires?|"
    r"now behaves?|behavior has changed|behaviour has changed|"
    r"behavior change|behaviour change|will now|used to|is now required|"
    r"has been removed|is no longer available|must now)\b", re.IGNORECASE,
)
DEPRECATION_RE = re.compile(
    r"\b(deprecat\w*|discouraged|should not be used)\b", re.IGNORECASE
)
LANGUAGE_FEATURES = (
    ("builtin-min-max", "Built-in min and max", re.compile(r"\bmin\b.*\bmax\b", re.I)),
    ("builtin-clear", "Built-in clear", re.compile(r"\bclear\b.*\b(map|slice)\b", re.I)),
    ("initialization-order", "Package initialization order", re.compile(r"initiali[sz]ation order", re.I)),
    ("type-inference", "Type inference improvements", re.compile(r"type inference|generic function", re.I)),
    ("nil-panic", "Nil panic and recover behavior", re.compile(r"panicnil|panic with a nil|recover.*nil|PanicNilError", re.I)),
    ("loopvar-preview", "Per-iteration loop variable preview", re.compile(r"loop variables per-iteration|LoopvarExperiment", re.I)),
)


def run_git(repo: Path, *args: str) -> str:
    """Run git in a repository and include stderr in any raised error."""
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
    """Resolve an initial release tag, accepting both Go tag spellings."""
    for tag in (f"go{version}.0", f"go{version}"):
        if ref_exists(repo, f"refs/tags/{tag}"):
            return tag
    raise RuntimeError(
        f"No initial release tag found for Go {version}; tried go{version}.0 and go{version}. "
        "Fetch tags with `git fetch --tags origin`."
    )


def git_file(repo: Path, ref: str, path: str) -> str | None:
    """Read a file at a git ref, returning None when it is absent."""
    exists = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    return run_git(repo, "show", f"{ref}:{path}") if exists else None


def extract_issue_numbers(text: str) -> list[str]:
    return sorted(set(ISSUE_RE.findall(text)))


def markdown_text(text: str) -> str:
    """Strip Markdown decoration without changing underscores in code/symbols."""
    text = HTML_COMMENT_RE.sub(" ", text)
    code: list[str] = []

    def preserve_code(match: re.Match[str]) -> str:
        code.append(match.group(2))
        return f"\x00CODE{len(code) - 1}\x00"

    text = re.sub(r"(`+)(.*?)\1", preserve_code, text, flags=re.DOTALL)
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = re.sub(r"</?(?:code|em|strong|span|a)\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?<!\w)(\*\*|__)(.+?)\1(?!\w)", r"\2", text)
    text = re.sub(r"(?<!\w)(\*|_)(.+?)\1(?!\w)", r"\2", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+.!-])", r"\1", text)
    text = " ".join(text.split()).strip()
    for index, value in enumerate(code):
        text = text.replace(f"\x00CODE{index}\x00", value)
    return text


def _note_issue_ids(raw: str) -> set[str]:
    """Extract issue links, ignoring rollback/revert bookkeeping comments."""
    issues: set[str] = set()
    for comment in HTML_COMMENT_RE.findall(raw):
        if re.search(r"\b(rolled back|reverted|rollback|revert)\b", comment, re.I):
            continue
        issues.update(extract_issue_numbers(comment))
    return issues


def parse_release_markdown(content: str) -> list[dict[str, Any]]:
    """Parse release-note Markdown into paragraph/list-item sized blocks."""
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if closing is not None:
            lines = lines[closing + 1:]

    heading = ""
    article_started = False
    sections: list[dict[str, Any]] = []
    block: list[str] = []
    pending_issues: set[str] = set()

    def flush() -> None:
        nonlocal pending_issues
        raw = "\n".join(block).strip()
        block.clear()
        if not raw:
            return

        issue_ids = _note_issue_ids(raw) | pending_issues
        body = HTML_COMMENT_RE.sub(" ", raw)
        text = markdown_text(body)
        if not text:
            pending_issues.update(issue_ids)
            return

        # A leading list marker is formatting, not part of the change summary.
        text = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", text)
        links = [url for _, url in MARKDOWN_LINK_RE.findall(body)]
        issue_ids.update(extract_issue_numbers(" ".join(links)))
        if article_started and heading:
            sections.append({
                "heading": heading,
                "text": text,
                "links": list(dict.fromkeys(links)),
                "issue_numbers": sorted(issue_ids),
            })
        pending_issues.clear()

    for line in lines:
        stripped = line.strip()
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if heading_match:
            flush()
            heading = HEADING_ID_RE.sub("", heading_match.group(1)).strip()
            heading = markdown_text(heading)
            if re.search(r"\bGo\s*1\.\d+\b", heading, re.I):
                article_started = True
            continue
        if not article_started:
            continue

        comment_only = HTML_COMMENT_RE.fullmatch(stripped)
        if comment_only:
            comment = comment_only.group(1)
            if not re.search(r"\b(rolled back|reverted|rollback|revert)\b", comment, re.I):
                pending_issues.update(extract_issue_numbers(comment))
            # Keep the comment with an open text/list item so item-local issue
            # references stay attached to that item, not the next paragraph.
            if block:
                block.append(stripped)
            continue

        list_match = LIST_ITEM_RE.match(line)
        if list_match:
            flush()
            block.append(list_match.group(1))
            continue
        if not stripped:
            flush()
        else:
            # Indented continuation lines remain part of the current list item.
            block.append(stripped)
    flush()
    return sections


def get_release_notes(repo: Path, tag: str, version: str,
                      website_repo: Path | None, website_ref: str) -> dict[str, Any]:
    """Load canonical notes from golang/website, where Go keeps release docs."""
    version_file = f"go{version}.md"
    if website_repo is not None:
        path = f"_content/doc/{version_file}"
        content = git_file(website_repo, website_ref, path)
        if content:
            sections = parse_release_markdown(content)
            if sections:
                return {"source": "golang/website", "path": path, "ref": website_ref,
                        "url": f"https://github.com/golang/website/blob/{website_ref}/{path}",
                        "sections": sections}
            raise RuntimeError(f"release-note file {path} at {website_ref} contained no article sections")

    for path in (f"_content/doc/{version_file}", f"doc/{version_file}"):
        content = git_file(repo, tag, path)
        if content:
            sections = parse_release_markdown(content)
            if sections:
                return {"source": "repository", "path": path,
                        "url": f"https://github.com/golang/go/blob/{tag}/{path}",
                        "sections": sections}

    raw_url = f"https://raw.githubusercontent.com/golang/website/{website_ref}/_content/doc/{version_file}"
    source_url = f"https://github.com/golang/website/blob/{website_ref}/_content/doc/{version_file}"
    try:
        request = Request(raw_url, headers={"User-Agent": "go-release-change-extractor/3.0"})
        with urlopen(request, timeout=30) as response:
            content = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        raise RuntimeError(
            f"cannot fetch official Go {version} release notes from {raw_url}: {exc}. "
            "Use --website-repo with a local golang/website checkout or check network access."
        ) from exc
    sections = parse_release_markdown(content)
    if not sections:
        raise RuntimeError(f"official release-note Markdown at {source_url} parsed to zero sections")
    return {"source": "golang/website", "path": f"_content/doc/{version_file}",
            "ref": website_ref, "url": source_url, "sections": sections}


def parse_api_file(text: str | None) -> set[str]:
    """Parse Go API inventory records, ignoring line numbers and comments."""
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


def api_inventories(repo: Path, tag: str, through_minor: int) -> dict[int, set[str]]:
    """Read each cumulative historical API inventory available at a tag."""
    inventories: dict[int, set[str]] = {}
    for minor in range(1, through_minor + 1):
        content = git_file(repo, tag, f"api/go1.{minor}.txt")
        if content is not None:
            inventories[minor] = parse_api_file(content)
    return inventories


def cumulative_api(inventories: dict[int, set[str]], through_minor: int) -> set[str]:
    return set().union(*(entries for minor, entries in inventories.items()
                         if minor <= through_minor)) if inventories else set()


def except_entries(repo: Path, tag: str) -> set[str]:
    """Read api/except.txt entries that Go permits to disappear compatibly."""
    return parse_api_file(git_file(repo, tag, "api/except.txt"))


def api_package(declaration: str) -> str:
    match = API_PACKAGE_RE.match(declaration)
    return match.group(1) if match else "unknown"


def canonical_api_declaration(declaration: str) -> str:
    """Remove platform qualifiers so architecture variants group together."""
    return ISSUE_SUFFIX_RE.sub("", PLATFORM_RE.sub("", declaration)).strip()


def grouped_api_records(declarations: set[str], category: str, url: str,
                        status: str = "evidence_extracted") -> list[dict[str, Any]]:
    """Group inventory lines by package and proposal issue, not one row each."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for declaration in declarations:
        package = api_package(declaration)
        issues = extract_issue_numbers(declaration)
        keys = issues or [canonical_api_declaration(declaration)]
        for key in keys:
            grouped.setdefault((package, key), []).append(declaration)

    records = []
    for (package, key), entries in sorted(grouped.items()):
        entries = sorted(set(entries))
        issue = key if key.isdigit() else None
        title = f"{package}: proposal #{issue}" if issue else entries[0]
        records.append({
            "category": category, "title": title,
            "summary": f"{len(entries)} API inventory declaration(s) for {package}.",
            "package": package, "declarations": entries,
            "issue_numbers": [issue] if issue else [], "status": status,
            "confidence": "high" if status == "evidence_extracted" else "candidate",
            "evidence": [{"type": "api_inventory", "url": url}],
        })
    return records


def api_change_records(repo: Path, base_tag: str, release_tag: str,
                       previous_minor: int, target_minor: int) -> list[dict[str, Any]]:
    """Compute API additions/deprecations/removal candidates cumulatively."""
    old_inventory = api_inventories(repo, base_tag, previous_minor)
    new_inventory = api_inventories(repo, release_tag, target_minor)
    old_api = cumulative_api(old_inventory, previous_minor)
    new_api = cumulative_api(new_inventory, target_minor)
    target_delta = new_inventory.get(target_minor, set())
    old_url = f"https://github.com/golang/go/blob/{base_tag}/api/go1.{previous_minor}.txt"
    new_url = f"https://github.com/golang/go/blob/{release_tag}/api/go1.{target_minor}.txt"

    additions = target_delta - old_api
    deprecated = {entry for entry in additions if "//deprecated" in entry.lower()}
    additions -= deprecated
    changes = grouped_api_records(additions, "public_api_addition", new_url)
    changes.extend(grouped_api_records(deprecated, "deprecation", new_url))

    exceptions = except_entries(repo, release_tag)
    removals = (old_api - new_api) - exceptions
    changes.extend(grouped_api_records(removals, "api_removal_candidate", old_url, "review_required"))
    for change in changes:
        if change["category"] == "api_removal_candidate":
            change["evidence"].extend([
                {"type": "api_inventory", "url": new_url},
                {"type": "compatibility_exceptions",
                 "url": f"https://github.com/golang/go/blob/{release_tag}/api/except.txt"},
            ])
    return changes


def commit_records(repo: Path, base_tag: str, release_tag: str,
                   paths: tuple[str, ...]) -> list[dict[str, Any]]:
    """Collect commits but exclude commits explicitly backported to maintenance."""
    raw = run_git(repo, "log", "--format=%H%x1f%s%x1f%b%x1e",
                  f"{base_tag}..{release_tag}", "--", *paths)
    records = []
    for row in raw.split("\x1e"):
        fields = row.strip("\n").split("\x1f", 2)
        if len(fields) != 3:
            continue
        sha, subject, body = fields
        sha = sha.strip()
        subject = subject.strip()
        # Maintenance backports can be ancestors of a later release tag. The
        # explicit subject prefix is Go's marker; do not attach them as release
        # implementation evidence.
        if re.match(r"^\[release-branch\.[^]]+\]", subject, re.I):
            continue
        files = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        records.append({
            "commit": sha, "subject": subject, "body": body.strip(),
            "issue_numbers": extract_issue_numbers(subject + "\n" + body),
            "files": files,
            "test_files": [path for path in files if "test" in Path(path).name.lower()],
            "url": f"https://github.com/golang/go/commit/{sha}",
        })
    return records


def matching_commits(note: dict[str, Any], commits: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Only link implementation evidence through exact issue-ID matches."""
    issues = set(note.get("issue_numbers", []))
    if not issues:
        return []
    matches = [commit for commit in commits if issues.intersection(commit["issue_numbers"])]
    return matches[:limit]


def note_evidence(note: dict[str, Any], notes_url: str) -> dict[str, Any]:
    return {"type": "release_note", "heading": note["heading"], "url": notes_url,
            "excerpt": note["text"], "links": note.get("links", [])}


def feature_key(text: str) -> tuple[str, str] | None:
    # Match the most specific clear feature first; min/max and clear can appear
    # in the same list block in older/flattened notes, so return all keys below.
    for key, title, pattern in LANGUAGE_FEATURES:
        if pattern.search(text):
            return key, title
    return None


def feature_keys(text: str) -> list[tuple[str, str]]:
    return [(key, title) for key, title, pattern in LANGUAGE_FEATURES if pattern.search(text)]


def normalized_heading(heading: str) -> str:
    return HEADING_ID_RE.sub("", heading.strip()).strip()


def package_for_heading(heading: str) -> str | None:
    """Map a package section heading to its source-tree path."""
    candidate = normalized_heading(heading).lower()
    if candidate.startswith("new ") or candidate in {
        "overview", "introduction to go", "tools", "go command", "cgo",
        "compiler", "assembler", "linker", "runtime", "standard library",
        "changes to the language", "darwin", "windows", "arm", "webassembly",
        "webassembly system interface", "ppc64/ppc64le", "loong64",
    }:
        return None
    if re.fullmatch(r"[a-z0-9_./-]+", candidate):
        return f"src/{candidate}/"
    return None


def _is_api_addition_note(note: dict[str, Any], api_changes: list[dict[str, Any]]) -> bool:
    """Avoid relabeling API inventory additions as behavior changes."""
    issues = set(note.get("issue_numbers", []))
    if not issues:
        return False
    for change in api_changes:
        if change["category"] not in {"public_api_addition", "deprecation"}:
            continue
        if issues.intersection(change.get("issue_numbers", [])):
            return True
    return False


def release_note_changes(notes: dict[str, Any], parser_commits: list[dict[str, Any]],
                         all_commits: list[dict[str, Any]],
                         api_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group language topics and focused release-note behavior/deprecation items."""
    language_groups: dict[str, dict[str, Any]] = {}
    note_groups: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}

    for note in notes["sections"]:
        heading = normalized_heading(note["heading"])
        text = note["text"].strip()
        issue_ids = set(note.get("issue_numbers", []))
        evidence = note_evidence(note, notes["url"])
        language_heading = heading.lower().startswith("changes to the language")

        if language_heading:
            for key, title in feature_keys(text):
                group = language_groups.setdefault(key, {
                    "category": "language_feature_candidate", "title": title,
                    "parts": [], "evidence": [], "issue_numbers": set(),
                    "matched": False,
                })
                relevant = matching_commits(note, parser_commits)
                group["matched"] = group["matched"] or bool(relevant)
                group["parts"].append(text)
                group["evidence"].append(evidence)
                group["issue_numbers"].update(issue_ids)
                group["evidence"].extend({
                    "type": "parser_typechecker_commit", "url": commit["url"],
                    "commit": commit["commit"], "subject": commit["subject"],
                    "files": commit["files"], "test_files": commit["test_files"],
                } for commit in relevant)

        category = "deprecation" if DEPRECATION_RE.search(text) else None
        if category is None and BEHAVIOR_SIGNALS.search(text):
            category = "behavior_change_candidate"
        if category is None or _is_api_addition_note(note, api_changes):
            continue

        # A release-note deprecation already represented by a deprecated API
        # inventory entry should not be duplicated, but keep unmatched notes.
        if category == "deprecation" and _is_api_addition_note(note, api_changes):
            continue

        package_path = package_for_heading(heading)
        candidates = [commit for commit in all_commits if package_path and
                      any(path.startswith(package_path) for path in commit["files"])]
        relevant = matching_commits(note, candidates)
        # Keep distinct issue-backed changes separate, rather than combining all
        # notes under a broad heading (for example several unrelated os changes).
        identity = tuple(sorted(issue_ids)) or (text,)
        group_key = (category, heading.lower(), identity)
        group = note_groups.setdefault(group_key, {
            "category": category, "title": heading, "parts": [], "evidence": [],
            "issue_numbers": set(), "status": "review_required", "confidence": "candidate",
        })
        group["parts"].append(text)
        group["evidence"].append(evidence)
        group["issue_numbers"].update(issue_ids)
        group["evidence"].extend({
            "type": "implementation_commit", "url": commit["url"],
            "commit": commit["commit"], "subject": commit["subject"],
            "files": commit["files"], "test_files": commit["test_files"],
        } for commit in relevant)

    results = []
    for key, group in language_groups.items():
        matched = group.pop("matched")
        group["category"] = "language_feature" if matched else "language_feature_candidate"
        group["status"] = "evidence_linked" if matched else "review_required"
        group["confidence"] = "high" if matched else "candidate"
        group["summary"] = " ".join(dict.fromkeys(group.pop("parts")))
        group["evidence"] = unique_evidence(group["evidence"])
        group["issue_numbers"] = sorted(group["issue_numbers"])
        results.append(group)
    for group in note_groups.values():
        group["summary"] = " ".join(dict.fromkeys(group.pop("parts")))
        group["evidence"] = unique_evidence(group["evidence"])
        group["issue_numbers"] = sorted(group["issue_numbers"])
        results.append(group)
    return results


def unique_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate evidence while keeping its original order."""
    result = []
    seen = set()
    for item in items:
        key = (item.get("type"), item.get("url"), item.get("excerpt"), item.get("commit"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def unique_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate records and assign stable IDs after sorting."""
    unique = {}
    for change in changes:
        key = (change["category"], change["title"], change["summary"])
        unique.setdefault(key, change)
    ordered = sorted(unique.values(), key=lambda item: (
        item["category"], item["title"], item["summary"]
    ))
    for index, change in enumerate(ordered, 1):
        change["id"] = f"change-{index:04d}"
    return ordered


def parse_version(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?:go)?(\d+)\.(\d+)", value)
    if not match:
        raise argparse.ArgumentTypeError("version must look like 1.21 or go1.21")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="local golang/go checkout")
    parser.add_argument("--version", required=True, type=parse_version, help="target version, e.g. 1.21")
    parser.add_argument("--output", type=Path, help="write JSON here; stdout if omitted")
    parser.add_argument("--website-repo", type=Path,
                        help="optional local golang/website checkout for offline/reproducible notes")
    parser.add_argument("--website-ref", default="master",
                        help="golang/website git ref (branch, tag, or commit); defaults to master")
    args = parser.parse_args()

    repo = args.repo.resolve()
    website_repo = args.website_repo.resolve() if args.website_repo else None
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
        notes = get_release_notes(repo, release_tag, version, website_repo, args.website_ref)
        if not notes["sections"]:
            raise RuntimeError("release-note parser returned no sections; refusing to emit an incomplete catalog")

        parser_commits = commit_records(repo, base_tag, release_tag, PARSER_TYPECHECK_PATHS)
        all_commits = commit_records(repo, base_tag, release_tag, ("src/",))
        changes = api_change_records(repo, base_tag, release_tag, previous_minor, target_minor)
        changes.extend(release_note_changes(notes, parser_commits, all_commits, changes))
        changes = unique_changes(changes)

        counts: dict[str, int] = {}
        for change in changes:
            counts[change["category"]] = counts.get(change["category"], 0) + 1

        output = {
            "schema_version": 7,
            "language": "Go", "version": version,
            "previous_version": previous_version,
            "base_tag": base_tag, "release_tag": release_tag,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scope": ["language_feature", "language_feature_candidate",
                      "public_api_addition", "deprecation",
                      "api_removal_candidate", "behavior_change_candidate"],
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
                "api_additions": "New target-release inventory entries grouped by package and proposal issue.",
                "api_removals": "Cumulative inventory difference filtered through api/except.txt; remaining entries are review candidates.",
                "deprecations": "Target API //deprecated markers and explicit release-note deprecation notices.",
                "language_features": "Only requested language topics are extracted; parser/type-checker implementation commits are linked only by exact issue ID.",
                "behavior_changes": "Explicit observable behavior statements from release notes; new API additions are not duplicated as behavior changes, and implementation evidence is linked only by exact issue ID.",
                "limitations": [
                    "Go release-note source is golang/website/_content/doc/go1.X.md, not the golang/go repository or rendered go.dev HTML.",
                    "Markdown parsing is block-oriented; unsupported formatting may require review.",
                    "Maintenance-branch commits with an explicit release-branch prefix are excluded from implementation evidence.",
                    "API removal records are candidates and require compatibility-policy review.",
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
