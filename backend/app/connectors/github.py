"""Normalize GitHub-shaped PR payloads (spec §6.1). Seed fixtures and future
`pull_request` / `pull_request_review` webhooks share this code. Never stores diffs."""
import re
from collections import Counter
from pathlib import PurePosixPath

from app.schemas import NormalizedEvent

COMMIT_TYPE_RE = re.compile(r"^(feat|fix|perf|refactor|test|docs|chore|build|ci)(\(.+\))?!?:")

LANGUAGES = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".rs": "Rust",
    ".rb": "Ruby", ".cpp": "C++", ".c": "C", ".sql": "SQL", ".yaml": "YAML", ".yml": "YAML",
    ".css": "CSS", ".scss": "CSS", ".sh": "Shell", ".tf": "Terraform",
}


def directory_of(path: str) -> str | None:
    """First min(3, depth-1) segments: services/payments/consumer/lag.py -> services/payments/consumer."""
    parts = PurePosixPath(path).parts
    keep = min(3, len(parts) - 1)
    return "/".join(parts[:keep]) if keep > 0 else None


def directories(files: list[str]) -> list[str]:
    return list(dict.fromkeys(d for f in files if (d := directory_of(f))))


def languages(files: list[str]) -> list[str]:
    return list(dict.fromkeys(LANGUAGES[s] for f in files if (s := PurePosixPath(f).suffix) in LANGUAGES))


def commit_types(commits: list[dict]) -> dict[str, int]:
    counts = Counter()
    for c in commits:
        m = COMMIT_TYPE_RE.match(c["message"])
        counts[m.group(1) if m else "other"] += 1
    return dict(counts)


def _fmt_commit_types(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {t}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))


class GitHubConnector:
    def normalize(self, pr: dict) -> list[NormalizedEvent]:
        dirs = directories(pr["files"])
        langs = languages(pr["files"])
        types = commit_types(pr.get("commits", []))
        base_meta = {"repo": pr["repo"], "pr_number": pr["number"], "files_changed": len(pr["files"]),
                     "directories": dirs, "languages": langs}
        body = (pr.get("body") or "")[:200]
        text = (f"PR: {pr['title']}. {body} | dirs: {', '.join(dirs)} | "
                f"commits: {_fmt_commit_types(types)} | langs: {', '.join(langs)}")
        events = [NormalizedEvent(
            source="github", kind="pr", external_id=f"{pr['repo']}#{pr['number']}",
            time=pr.get("merged_at") or pr["created_at"], person_id=pr["author"], direction="did",
            title=pr["title"], text=text[:800], metadata={**base_meta, "commit_types": types},
        )]
        for r in pr.get("reviews", []):
            events.append(NormalizedEvent(
                source="github", kind="review",
                external_id=f"{pr['repo']}#{pr['number']}/review/{r['reviewer']}/{r['submitted_at']}",
                time=r["submitted_at"], person_id=r["reviewer"], direction="did", title=pr["title"],
                text=f'Reviewed PR #{pr["number"]} "{pr["title"]}" in {", ".join(dirs)}'[:800],
                metadata=base_meta,
            ))
        return events
