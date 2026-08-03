"""Local-only, atomic cache for validated model proposals."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


class ProposalCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def key(
        self,
        arxiv_id: str,
        profile_version: int,
        prompt_version: str,
        model: str,
        candidate_fingerprint: str,
    ) -> str:
        return hashlib.sha256(
            (
                f"{arxiv_id}\0{profile_version}\0{prompt_version}\0{model}\0{candidate_fingerprint}"
            ).encode()
        ).hexdigest()

    def layered_key(
        self,
        *,
        layer: str,
        arxiv_id: str,
        candidate_fingerprint: str,
        protected_profile_digest: str,
        evidence_snapshot: str,
        contract_version: str,
        model: str,
        output_language: str,
    ) -> str:
        """Namespace judge and explanation caches by every ranking-relevant input version."""

        values = (
            layer,
            arxiv_id,
            candidate_fingerprint,
            protected_profile_digest,
            evidence_snapshot,
            contract_version,
            model,
            output_language,
        )
        if any(not value.strip() for value in values):
            raise ValueError("layered cache inputs must not be empty")
        return hashlib.sha256("\0".join(values).encode()).hexdigest()

    def get(self, key: str) -> str | None:
        data = self._read()
        value = data.get(key)
        return value if isinstance(value, str) else None

    def put(self, key: str, proposal: str) -> None:
        data = self._read()
        data[key] = proposal
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _read(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
