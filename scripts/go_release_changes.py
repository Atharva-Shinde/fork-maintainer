#!/usr/bin/env python3
"""Extract a focused, evidence-linked Go release change catalog as JSON.

Only language features, public API additions, deprecations, API removal
candidates, and documented behavior changes are included. The extractor reads
tagged API inventories and release notes, then links relevant implementation
commits and tests where they can be identified. It does not label arbitrary
commits or inspect maintenance-branch history.

Requires a local golang/go checkout with release tags. Uses the Python standard
library and git. Official release notes are fetched from go.dev when they are
not present in the tagged checkout.
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
API_PACKAGE_RE = re.compile(r"^pkg\s+([^,\s]+)")
ISSUE_SUFFIX_RE = re.compile(r"\s+#\d{4,}\s*$")
PLATFORM_RE = re.compile(r"\s+\([^)]+(?:-[^)]+)*\)(?=,)")
PARSER_TYPECHECK_PATHS = (
    "src/go/parser/", "src/go/types/",
    "src/cmd/compile/internal/syntax/", "src/cmd/compile/internal/types2/",
)
HIDDEN_TAGS = {"nav", "header", "footer", "script", "style", "noscript"}
# Keep behavior extraction conservative: these describe an observable change,
# not merely a new API or an implementation/performance improvement.
BEHAVIOR_SIGNALS = re.compile(
    r"\b(no longer|previously|instead of|now returns?|now requires?|"
    r"now behaves?|behavior has changed|behaviour has changed|"
    r"behavior change|behaviour change|will now|used to|is now required|"
    r"has been removed|is no longer available)\b", re.IGNORECASE,
)
DEPRECATION_RE = re.compile(
    r"\b(deprecat\w*|discouraged|should not be used)\b", re.IGNORECASE
)
# Go 1.21 language claims requested for the initial focused extraction. The
# patterns intentionally match the feature itself, not every paragraph under
# the broad "Changes to the language" heading.
LANGUAGE_FEATURES = (
    ("builtin-min-max", "Built-in min and max", re.compile(r"\bmin\b.*\bmax\b", re.I)),
    ("builtin-clear", "Built-in clear", re.compile(r"\bclear\b.*\b(map|slice)\b", re.I)),
    ("initialization-order", "Package initialization order", re.compile(r"initiali[sz]ation order", re.I)),
    ("type-inference", "Type inference improvements", re.compile(r"type inference|generic function", re.I)),
    ("nil-panic", "Nil panic and recover behavior", re.compile(r"panicnil|panic with a nil|recover.*nil|PanicNilError", re.I)),
    ("loopvar-preview", "Per-iteration loop variable preview", re.compile(r"loop variables per-iteration|LoopvarExperiment", re.I)),
)


def run_git(repo: Path, *args: str) -> str:
    """Run git in the supplied checkout and turn failures into useful errors."""
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
    """Return a file at a revision, or None when that path is absent."""
    if subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode:
        return None
    return run_git(repo, "show", f"{ref}:{path}")


def extract_issue_numbers(text: str) -> list[str]:
    return sorted(set(ISSUE_RE.findall(text)))


class ReleaseNotesParser(HTMLParser):
    """Read only the release-note article and ignore site chrome and footer."""
    BLOCK_TAGS = {"p", "li", "pre", "blockquote"}

    def __init__(self, version: str) -> None:
        super().__init__(convert_charrefs=True)
        self.version = version
        self.main_depth = 0
        self.hidden_depth = 0
        self.article_started = False
        self.heading = ""
        self.heading_level = 0
        self.heading_parts: list[str] = []
        self.block_tag: str | None = None
        self.block_depth = 0
        self.block_parts: list[str] = []
        self.block_links: list[str] = []
        self.sections: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag in HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if tag == "main":
            self.main_depth += 1
        if self.hidden_depth:
            return
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
            href = attr_map.get("href")
            if href:
                self.block_links.append(href)

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if self.heading_level:
            self.heading_parts.append(data)
        if self.block_tag is not None:
            self.block_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if re.fullmatch(r"h[1-6]", tag) and self.heading_level:
            title = " ".join("".join(self.heading_parts).split())
            self.heading = title
            # The first release-specific heading starts the article. This also
            # makes parsing resilient when a site template has no <main> tag.
            if re.search(rf"\bgo\s*{re.escape(self.version)}\b", title, re.I):
                self.article_started = True
            self.heading_level = 0
        elif tag == "main" and self.main_depth:
            self.main_depth -= 1
        elif self.block_tag is not None and tag in self.BLOCK_TAGS:
            self.block_depth -= 1
            if self.block_depth <= 0:
                text = " ".join("".join(self.block_parts).split())
                if text and self.article_started and self.heading:
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


def parse_markdown_notes(content: str, version: str) -> list[dict[str, Any]]:
    """Parse release-note Markdown into paragraph-sized evidence blocks."""
    heading = ""
    article_started = False
    sections: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush() -> None:
        text = " ".join(" ".join(paragraph).split())
        if article_started and heading and text:
            sections.append({"heading": heading, "text": text, "links": [],
                             "issue_numbers": extract_issue_numbers(text)})
        paragraph.clear()

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("# ")
            if re.search(rf"\bgo\s*{re.escape(version)}\b", heading, re.I):
                article_started = True
        elif stripped:
            paragraph.append(stripped)
        else:
            flush()
    flush()
    return sections


def get_release_notes(repo: Path, tag: str, version: str) -> dict[str, Any]:
    """Read tagged notes first; fetch the official page only as a fallback."""
    url = f"https://go.dev/doc/go{version}"
    for path in (f"doc/go{version}.html", f"doc/go{version}.md"):
        content = git_file(repo, tag, path)
        if content is None:
            continue
        if path.endswith(".html"):
            parser = ReleaseNotesParser(version)
            parser.feed(content)
            sections = parser.sections
        else:
            sections = parse_markdown_notes(content, version)
        return {"source": "repository", "path": path,
                "url": f"https://github.com/golang/go/blob/{tag}/{path}",
                "sections": sections}

    try:
        request = Request(url, headers={"User-Agent": "go-release-change-extractor/1.0"})
        with urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8", errors="replace")
        parser = ReleaseNotesParser(version)
        parser.feed(content)
        if not parser.sections:
            raise RuntimeError("could not find the release-note article in the downloaded page")
        return {"source": "go.dev", "path": None, "url": url,
                "sections": parser.sections}
    except (OSError, URLError, RuntimeError) as exc:
        return {"source": "unavailable", "path": None, "url": url,
                "sections": [], "error": str(exc)}


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
        # An issue is the proposal-level key; use the canonical declaration
        # where inventories do not carry a proposal issue number.
        keys = issues or [canonical_api_declaration(declaration)]
        for issue in keys:
            grouped.setdefault((package, issue), []).append(declaration)

    records = []
    for (package, key), entries in sorted(grouped.items()):
        entries = sorted(set(entries))
        issue = key if key.isdigit() else None
        title = f"{package}: proposal #{issue}" if issue else entries[0]
        records.append({
            "category": category,
            "title": title,
            "summary": f"{len(entries)} API inventory declaration(s) for {package}.",
            "package": package,
            "declarations": entries,
            "issue_numbers": [issue] if issue else [],
            "status": status,
            "confidence": "high" if status == "evidence_extracted" else "candidate",
            "evidence": [{"type": "api_inventory", "url": url}],
        })
    return records


def api_change_records(repo: Path, base_tag: str, release_tag: str,
                       previous_minor: int, target_minor: int) -> list[dict[str, Any]]:
    """Compute additions/deprecations/removal candidates from cumulative APIs."""
    old_inventories = api_inventories(repo, base_tag, previous_minor)
    new_inventories = api_inventories(repo, release_tag, target_minor)
    old_api = cumulative_api(old_inventories, previous_minor)
    new_api = cumulative_api(new_inventories, target_minor)
    target_delta = new_inventories.get(target_minor, set())

    old_url = f"https://github.com/golang/go/blob/{base_tag}/api/go1.{previous_minor}.txt"
    new_url = f"https://github.com/golang/go/blob/{release_tag}/api/go1.{target_minor}.txt"
    changes: list[dict[str, Any]] = []

    additions = target_delta - old_api
    deprecated = {entry for entry in additions if "//deprecated" in entry.lower()}
    additions -= deprecated
    changes.extend(grouped_api_records(additions, "public_api_addition", new_url))
    changes.extend(grouped_api_records(deprecated, "deprecation", new_url))

    # API inventories are intended to be cumulative. Filter Go's explicit
    # compatibility exceptions before exposing remaining differences.
    allowed_removals = except_entries(repo, release_tag)
    removal_candidates = (old_api - new_api) - allowed_removals
    changes.extend(grouped_api_records(
        removal_candidates, "api_removal_candidate", old_url, "review_required"
    ))
    # Attach the target inventory as comparative evidence to each removal group.
    for change in changes:
        if change["category"] == "api_removal_candidate":
            change["evidence"].append({"type": "api_inventory", "url": new_url})
            change["evidence"].append({
                "type": "compatibility_exceptions",
                "url": f"https://github.com/golang/go/blob/{release_tag}/api/except.txt",
            })
    return changes


def commit_records(repo: Path, base_tag: str, release_tag: str,
                   paths: tuple[str, ...]) -> list[dict[str, Any]]:
    """Collect bounded commit metadata and diff paths for relevant source dirs."""
    raw = run_git(repo, "log", "--format=%H%x1f%s%x1f%b", f"{base_tag}..{release_tag}", "--", *paths)
    records = []
    for row in raw.split("\n\n"):
        fields = row.split("\x1f", 2)
        if len(fields) != 3:
            continue
        sha, subject, body = fields
        sha = sha.strip()
        files = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        records.append({
            "commit": sha,
            "subject": subject.strip(),
            "body": body.strip(),
            "issue_numbers": extract_issue_numbers(subject + "\n" + body),
            "files": files,
            "test_files": [path for path in files if "test" in Path(path).name.lower()],
            "url": f"https://github.com/golang/go/commit/{sha}",
        })
    return records


def diff_tokens(text: str) -> set[str]:
    """Extract meaningful words for cautious release-note/commit matching."""
    stop = {"about", "after", "also", "because", "before", "being", "calls", "changes",
            "does", "from", "have", "into", "more", "only", "other", "that", "their",
            "then", "there", "these", "this", "through", "under", "using", "were", "what",
            "when", "where", "which", "with", "would", "your", "now", "new", "used"}
    return {word for word in re.findall(r"[a-z][a-z0-9_]{3,}", text.lower()) if word not in stop}


def matching_commits(note: dict[str, Any], commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match note evidence to commits by issue first, then strong word overlap."""
    issues = set(note.get("issue_numbers", []))
    note_terms = diff_tokens(note["text"])
    matches = []
    for commit in commits:
        if issues and issues.intersection(commit["issue_numbers"]):
            matches.append(commit)
            continue
        commit_terms = diff_tokens(commit["subject"] + " " + commit["body"])
        shared = note_terms & commit_terms
        if len(shared) >= 3:
            matches.append(commit)
    return matches[:10]


def note_evidence(note: dict[str, Any], notes_url: str) -> dict[str, Any]:
    return {"type": "release_note", "heading": note["heading"],
            "url": notes_url, "excerpt": note["text"], "links": note.get("links", [])}


def feature_key(text: str) -> tuple[str, str] | None:
    for key, title, pattern in LANGUAGE_FEATURES:
        if pattern.search(text):
            return key, title
    return None


def package_for_heading(heading: str) -> str | None:
    """Translate release-note package headings to Go source path prefixes."""
    candidate = heading.strip().lower()
    if candidate.startswith("new ") or candidate in {"overview", "introduction to go"}:
        return None
    if candidate.startswith("changes to the language") or candidate in {"language", "tools"}:
        return None
    if re.fullmatch(r"[a-z0-9_./-]+", candidate) and "/" in candidate:
        return f"src/{candidate}/"
    if re.fullmatch(r"[a-z][a-z0-9_]*", candidate):
        return f"src/{candidate}/"
    return None


def release_note_changes(notes: dict[str, Any], parser_commits: list[dict[str, Any]],
                         all_commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group release-note evidence into focused language/deprecation/behavior items."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for note in notes["sections"]:
        heading = note["heading"].strip()
        text = note["text"].strip()
        evidence = note_evidence(note, notes["url"])
        issue_ids = set(note.get("issue_numbers", []))

        language_heading = heading.lower().startswith("changes to the language") or heading.lower() == "language"
        feature = feature_key(text) if language_heading else None
        if feature:
            key, title = feature
            relevant = matching_commits(note, parser_commits)
            # Some release notes (notably Go language changes) contain no issue
            # IDs. Keep the note as a reviewable candidate unless code evidence
            # can be linked by parser/type-checker paths and related terms.
            category = "language_feature" if relevant else "language_feature_candidate"
            record_key = (category, key)
            group = grouped.setdefault(record_key, {
                "category": category, "title": title, "parts": [], "evidence": [],
                "issue_numbers": set(), "status": "evidence_linked" if relevant else "review_required",
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

        category: str | None = None
        if DEPRECATION_RE.search(text):
            category = "deprecation"
        elif BEHAVIOR_SIGNALS.search(text):
            category = "behavior_change_candidate"
        if not category:
            continue

        package_path = package_for_heading(heading)
        candidates = [c for c in all_commits
                      if package_path and any(path.startswith(package_path) for path in c["files"])]
        relevant = matching_commits(note, candidates)
        record_key = (category, heading.lower())
        group = grouped.setdefault(record_key, {
            "category": category, "title": heading,
            "parts": [], "evidence": [], "issue_numbers": set(),
            "status": "review_required", "confidence": "candidate",
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
        # Deduplicate repeated paragraphs and evidence while preserving order.
        group["summary"] = " ".join(dict.fromkeys(group.pop("parts")))
        group["evidence"] = unique_evidence(group["evidence"])
        group["issue_numbers"] = sorted(group["issue_numbers"])
        results.append(group)
    return results


def unique_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one copy of each evidence item in stable order."""
    result = []
    seen = set()
    for item in items:
        key = (item.get("type"), item.get("url"), item.get("excerpt"), item.get("commit"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def unique_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate grouped records and assign stable IDs after sorting."""
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for change in changes:
        key = (change["category"], change["title"], change["summary"])
        if key not in unique:
            unique[key] = change
    ordered = sorted(unique.values(), key=lambda item: (
        item["category"], item["title""], item["summary"]
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
        parser_commits = commit_records(repo, base_tag, release_tag, PARSER_TYPECHECK_PATHS)
        all_commits = commit_records(repo, base_tag, release_tag, ("src/",))
        changes = api_change_records(repo, base_tag, release_tag, previous_minor, target_minor)
        changes.extend(release_note_changes(notes, parser_commits, all_commits))
        changes = unique_changes(changes)

        counts: dict[str, int] = {}
        for change in changes:
            counts[change["category"]] = counts.get(change["category"], 0) + 1

        output = {
            "schema_version": 4,
            "language": "Go",
            "version": version,
            "previous_version": previous_version,
            "base_tag": base_tag,
            "release_tag": release_tag,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scope": ["language_feature", "public_api_addition", "deprecation",
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
                "api_removals": "Cumulative inventory difference filtered through api/except.txt; remaining entries are manual-review candidates.",
                "deprecations": "Target API //deprecated markers and explicit release-note deprecation notices, grouped by package.",
                "language_features": "Only requested language topics are extracted; parser/type-checker commit evidence is linked when relevant commits can be matched.",
                "behavior_changes": "Explicit compatibility/behavior statements from release notes, linked to relevant package commits and tests when identifiable.",
                "limitations": [
                    "Issue links are not present in every release note; unmatched evidence remains review_required.",
                    "API removals are candidates and must be checked against Go compatibility policy.",
                    "Commit matching is conservative and may omit evidence rather than create a weak association.",
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
