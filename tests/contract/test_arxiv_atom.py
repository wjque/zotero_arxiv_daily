from __future__ import annotations

import pytest

from zotero_arxiv_daily.arxiv.atom import parse_feed
from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id
from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError

_FEED = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2401.01234v2</id><updated>2026-08-01T00:00:00Z</updated><published>2026-07-31T00:00:00Z</published><title> Synthetic\n paper </title><summary> Safe public abstract </summary><author><name>Ada</name></author><category term="cs.LG"/></entry></feed>"""


def test_parse_atom_feed_preserves_public_dates_and_canonical_revision() -> None:
    candidate = parse_feed(_FEED)[0]

    assert candidate.arxiv_id.canonical == "2401.01234"
    assert candidate.arxiv_id.revision == 2
    assert candidate.title == "Synthetic paper"
    assert candidate.abstract_url == "https://arxiv.org/abs/2401.01234"


def test_parse_atom_rejects_malformed_xml() -> None:
    with pytest.raises(ExternalServiceError, match="malformed"):
        parse_feed(b"<feed>")


def test_parse_empty_atom_feed_returns_no_candidates() -> None:
    assert parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"/>') == ()


def test_parse_arxiv_id_supports_legacy_ids_and_rejects_invalid_ids() -> None:
    assert parse_arxiv_id("hep-th/9901001v3").canonical == "hep-th/9901001"
    with pytest.raises(ConfigurationError):
        parse_arxiv_id("not-an-id")
