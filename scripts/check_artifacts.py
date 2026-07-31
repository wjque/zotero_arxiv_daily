"""Fail CI if tracked text contains common credential material or private Zotero exports."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "DeepSeek key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "Zotero export": re.compile(r'"itemType"\s*:\s*"(?:attachment|annotation|note)"'),
}


def main() -> int:
    """Scan tracked UTF-8 text files without printing matching contents."""

    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True, text=False)
    violations: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8"))
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in _PATTERNS.items():
            if label == "Zotero export" and path.suffix not in {".json", ".jsonl", ".rdf"}:
                continue
            if pattern.search(content):
                violations.append(f"{path}: possible {label}")
    if violations:
        print("Artifact safety check failed:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("Artifact safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
