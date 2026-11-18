#!/usr/bin/env python3
"""Extract evidence-linked Go release changes as JSON.

The catalog covers language features, public API additions, deprecations, API
removal candidates, and documented behavior changes. It compares tagged Go API
inventories and parses the official release-note Markdown from golang/website.
Relevant implementation commits and test files are attached when they can be
identified. Arbitrary commits and maintenance-branch history are not classified.

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

# Explicit observable-change language only; avoid treating new APIs or speedups
# as behavior changes merely because a note says "now" or "new".
BEHAVIOR_SIGNALS = re.compile(
    r"\b(no longer|previously|instead of|now returns?|now requires?|"
    r"now behaves?|behavior has changed|behaviour has changed|"
    r"behavior change|behaviour change|will now|used to|is now required|"
    r"has been removed|is no longer available)\b", re.IGNORECASE,
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
    """Remove Markdown/HTML presentation while preserving readable wording."""
    text = HTML_COMMENT_RE.sub(" ", text)
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = re.sub(r"`+", "", text)
    text = re.sub(r"</?\w+[^>]*>", " ", text)
    text = re.sub(r"[*_~]", "", text)
    return " ".join(text.split()).strip()


def parse_release_markdown(content: str) -> list[dict[str, Any]]:
    """Parse official release-note Markdown, retaining headings, links/issues.

    Go release notes are maintained in golang/website as Markdown, not in the
    golang/go source tree. Issue links often live in HTML comments immediately
    before a paragraph or list item, so carry those IDs onto the next text block.
    """
    lines = content.splitlines()
    # Skip YAML front matter; its title/path are metadata, not article content.
    if lines and lines[0].strip() == "---":
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if closing is not None:
            lines = lines[closing + 1:]

    heading = ""
    article_started = False
    sections: list[dict[str, Any]] = []
    paragraph: list[str] = []
    pending_issues: set[str] = set()

    def flush() -> None:
        nonlocal pending_issues
        raw = "\n".join(paragraph).strip()
        paragraph.clear()
        if not raw:
            return

        # Comments contain issue references and sometimes CL references. Strip
        # them from display text but retain the issue links as evidence.
        comment_issues = set()
        for comment in HTML_COMMENT_RE.findall(raw):
            comment_issues.update(extract_issue_numbers(comment))
        body = HTML_COMMENT_RE.sub(" ", raw)
        text = markdown_text(body)
        if not text:
            pending_issues.update(comment_issues)
            return

        links = list(dict.fromkeys(MARKDOWN_LINK_RE.findall(body)))
        # Store URLs, not link label/URL tuples, while retaining issue IDs from
        # both visible prose and source comments.
        link_urls = [url for _, url in links]
        issues = pending_issues | comment_issues
        issues.update(extract_issue_numbers(raw + " " + " ".join(link_urls)))
        if article_started and heading:
            sections.append({
                "heading": heading,
                "text": text,
                "links": link_urls,
                "issue_numbers": sorted(issues),
            })
        pending_issues = set()

    for line in lines:
        stripped = line.strip()
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*#\{[^}]+\}\s*$", stripped)
        if not heading_match:
            heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if heading_match:
            flush()
            heading = markdown_text(heading_match.group(1))
            if re.search(r"\bGo\s*1\.\d+\b", heading, re.I):
                article_started = True
            continue

        if not article_started:
            continue
        # An issue-only comment can be separated from its note by blank lines.
        comment_only = HTML_COMMENT_RE.fullmatch(stripped)
        if comment_only:
            pending_issues.update(extract_issue_numbers(comment_only.group(1)))
            continue
        if not stripped:
            flush()
        else:
            paragraph.append(stripped)
    flush()
    return sections


def get_release_notes(repo: Path, tag: str, version: str,
                      website_repo: Path | None, website_ref: str) -> dict[str, Any]:
    """Load canonical notes from golang/website, where Go keeps release docs."""
    version_file = f"go{version}.md"

    # A local website checkout is deterministic and avoids a network request.
    if website_repo is not None:
        path = f"_content/doc/{version_file}"
        content = git_file(website_repo, website_ref, path)
        if content:
            sections = parse_release_markdown(content)
            if sections:
                return {
                    "source": "golang/website",
                    "path": path,
                    "ref": website_ref,
                    "url": f"https://github.com/golang/website/blob/{website_ref}/{path}",
                    "sections": sections,
                }
            raise RuntimeError(f"release-note file {path} at {website_ref} contained no article sections")

    # Check the Go checkout only for a local copy. The official notes were moved
    # out of golang/go to golang/website (golang/go#58645), so absence here is
    # expected rather than evidence that the version has no release notes.
    for path in (f"_content/doc/{version_file}", f"doc/{version_file}"):
        content = git_file(repo, tag, path)
        if content:
            sections = parse_release_markdown(content)
            if sections:
                return {"source": "repository", "path": path,
                        "url": f"https://github.com/golang/go/blob/{tag}/{path}",
                        "sections": sections}

    # The canonical source is raw Markdown, not the rendered go.dev HTML page.
    # The latter includes site navigation and has a different DOM structure.
    raw_url = f"https://raw.githubusercontent.com/golang/website/{website_ref}/_content/doc/{version_file}"
    source_url = f"https://github.com/golang/website/blob/{website_ref}/_content/doc/{version_file}"
    try:
        request = Request(raw_url, headers={"User-Agent": "go-release-change-extractor/2.0"})
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

    # Go's explicit compatibility exceptions are not API removals.
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
    """Collect commit metadata and changed/test files for source paths."""
    raw = run_git(repo, "log", "--format=%H%x1f%s%x1f%b",
                  f"{base_tag}..{release_tag}", "--", *paths)
    records = []
    for row in raw.split("\n\n"):
        fields = row.split("\x1f", 2)
        if len(fields) != 3:
            continue
        sha, subject, body = fields
        sha = sha.strip()
        files = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        records.append({
            "commit": sha, "subject": subject.strip(), "body": body.strip(),
            "issue_numbers": extract_issue_numbers(subject + "\n" + body),
            "files": files,
            "test_files": [path for path in files if "test" in Path(path).name.lower()],
            "url": f"https://github.com/golang/go/commit/{sha}",
        })
    return records


def diff_tokens(text: str) -> set[str]:
    """Extract useful words for conservative note/commit matching."""
    stop = {"about", "after", "also", "because", "before", "being", "calls", "changes",
            "does", "from", "have", "into", "more", "only", "other", "that", "their",
            "then", "there", "these", "this", "through", "under", "using", "were", "what",
            "when", "where", "which", "with", "would", "your", "now", "new", "used"}
    return {word for word in re.findall(r"[a-z][a-z0-9_]{3,}", text.lower()) if word not in stop}


def matching_commits(note: dict[str, Any], commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match note to commits by linked issue, or substantial term overlap."""
    issues = set(note.get("issue_numbers", []))
    note_terms = diff_tokens(note["text"])
    matches = []
    for commit in commits:
        if issues.intersection(commit["issue_numbers"]):
            matches.append(commit)
            continue
        if len(note_terms & diff_tokens(commit["subject"] + " " + commit["body"])) >= 3:
            matches.append(commit)
    return matches[:10]


def note_evidence(note: dict[str, Any], notes_url: str) -> dict[str, Any]:
    return {"type": "release_note", "heading": note["heading"], "url": notes_url,
            "excerpt": note["text"], "links": note.get("links", [])}


def feature_key(text: str) -> tuple[str, str] | None:
    for key, title, pattern in LANGUAGE_FEATURES:
        if pattern.search(text):
            return key, title
    return None


def package_for_heading(heading: str) -> str | None:
    """Map a package section heading to its source-tree path."""
    candidate = heading.strip().lower()
    if candidate.startswith("new ") or candidate in {"overview", "introduction to go", "tools", "go command", "cgo", "compiler", "assembler", "linker", "runtime", "standard library", "changes to the language"}:
        return None
    # Release note headings can be Markdown links such as [archive/tar](/pkg/...).
    candidate = markdown_text(candidate)
    if re.fullmatch(r"[a-z0-9_./-]+", candidate):
        return f"src/{candidate}/"
    return None


def release_note_changes(notes: dict[str, Any], parser_commits: list[dict[str, Any]],
                         all_commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group language topics and release-note behavior/deprecation candidates."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for note in notes["sections"]:
        heading = note["heading"].strip()
        text = note["text"].strip()
        evidence = note_evidence(note, notes["url"])
        issue_ids = set(note.get("issue_numbers", []))

        language_heading = heading.lower().startswith("changes to the language")
        feature = feature_key(text) if language_heading else None
        if feature:
            key, title = feature
            relevant = matching_commits(note, parser_commits)
            category = "language_feature" if relevant else "language_feature_candidate"
            record_key = (category, key)
            group = grouped.setdefault(record_key, {
                "category": category, "title": title, "parts": [], "evidence": [],
                "issue_numbers": set(),
                "status": "evidence_linked" if relevant else "review_required",
                "confidence": "high" if relevant else "candidate",
            })
            group["parts"].append(text)
            group["evidence"].append(evidence)
            group["issue_numbers"].update(issue_ids)
            group["evidence"].extend({
                "type": "parser_typechecker_commit", "url": commit["url"],
                "commit": commit["commit"], "subject": commit["subject"],
                "files": commit["files"], "test_files": commit["test_files"],
            } for commit in relevant)
            continue

        category = "deprecation" if DEPRECATION_RE.search(text) else None
        if category is None and BEHAVIOR_SIGNALS.search(text):
            category = "behavior_change_candidate"
        if category is None:
            continue

        package_path = package_for_heading(heading)
        candidates = [commit for commit in all_commits if package_path and
                      any(path.startswith(package_path) for path in commit["files"])]
        relevant = matching_commits(note, candidates)
        record_key = (category, heading.lower())
        group = grouped.setdefault(record_key, {
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
    for group in grouped.values():
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
        changes.extend(release_note_changes(notes, parser_commits, all_commits))
        changes = unique_changes(changes)

        counts: dict[str, int] = {}
        for change in changes:
            counts[change["category"]] = counts.get(change["category"], 0) + 1

        output = {
            "schema_version": 6,
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
                "language_features": "Only the requested language topics are extracted; parser/type-checker evidence is issue-linked when available.",
                "behavior_changes": "Explicit observable behavior statements from release notes; records remain review_required and include related implementation/test evidence when identified.",
                "limitations": [
                    "Go release-note source is golang/website/_content/doc/go1.X.md, not the golang/go repository or rendered go.dev HTML.",
                    "Unmatched commit evidence is omitted rather than weakly associated.",
                    "API removal records are candidates and require compatibility-policy review.",
                    "This output excludes maintenance-branch patch history.",
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
