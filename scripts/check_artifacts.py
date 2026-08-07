"""Fail CI if tracked text contains common credential material or private Zotero exports."""

from __future__ import annotations

import gzip
import re
import subprocess
import sys
from pathlib import Path

_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "DeepSeek key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "Zotero export": re.compile(r'"itemType"\s*:\s*"(?:attachment|annotation|note)"'),
    "local absolute path": re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\\\Users\\\\)"),
    "raw model prompt": re.compile(r'"(?:prompt|response_body|raw_profile)"\s*:'),
}

_SITE_SIZE_BUDGETS = {
    "index.html": 8 * 1024,
    "assets/site.css": 8 * 1024,
    "assets/app.js": 24 * 1024,
}
_SITE_TOTAL_BUDGET = 48 * 1024
_SITE_GZIP_BUDGETS = {
    "index.html": 3 * 1024,
    "assets/site.css": 3 * 1024,
    "assets/app.js": 8 * 1024,
}
_SITE_GZIP_TOTAL_BUDGET = 14 * 1024


def main() -> int:
    """Scan tracked UTF-8 text files without printing matching contents."""

    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True, text=False)
    violations: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8"))
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in _PATTERNS.items():
            if path == Path("scripts/check_artifacts.py") and label in {
                "local absolute path",
                "raw model prompt",
            }:
                continue
            if label == "Zotero export" and path.suffix not in {".json", ".jsonl", ".rdf"}:
                continue
            if pattern.search(content):
                violations.append(f"{path}: possible {label}")
    site = Path("runtime/site")
    if site.is_dir():
        total = 0
        compressed_total = 0
        for relative, budget in _SITE_SIZE_BUDGETS.items():
            artifact = site / relative
            if not artifact.is_file():
                violations.append(f"{artifact}: required site artifact is missing")
                continue
            size = artifact.stat().st_size
            total += size
            if size > budget:
                violations.append(f"{artifact}: {size} bytes exceeds {budget}-byte budget")
            compressed_size = len(gzip.compress(artifact.read_bytes()))
            compressed_total += compressed_size
            compressed_budget = _SITE_GZIP_BUDGETS[relative]
            if compressed_size > compressed_budget:
                violations.append(
                    f"{artifact}: {compressed_size} gzip bytes exceeds "
                    f"{compressed_budget}-byte budget"
                )
        if total > _SITE_TOTAL_BUDGET:
            violations.append(f"runtime/site: {total} bytes exceeds total asset budget")
        if compressed_total > _SITE_GZIP_TOTAL_BUDGET:
            violations.append(
                f"runtime/site: {compressed_total} gzip bytes exceeds total asset budget"
            )
        for artifact in site.rglob("*"):
            if not artifact.is_file():
                continue
            try:
                content = artifact.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in _PATTERNS.items():
                if pattern.search(content):
                    violations.append(f"{artifact}: possible {label}")
    if violations:
        print("Artifact safety check failed:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("Artifact safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
