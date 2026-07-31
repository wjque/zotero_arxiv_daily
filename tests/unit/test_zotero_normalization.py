from __future__ import annotations

from zotero_arxiv_daily.zotero.normalization import normalize_item, normalize_text


def test_normalize_text_removes_markup_script_and_normalizes_whitespace() -> None:
    assert normalize_text("<p>Ａ　study</p><script>ignore()</script><p>next</p>") == "A study next"


def test_normalize_item_keeps_manual_and_automatic_tags_distinct() -> None:
    item = normalize_item(
        {
            "key": "PAPER001",
            "version": 4,
            "data": {
                "itemType": "journalArticle",
                "title": "Synthetic paper",
                "tags": [{"tag": "manual", "type": 0}, {"tag": "automatic", "type": 1}],
                "collections": ["COLL0001"],
                "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
                "DOI": "10.1000/example",
            },
        }
    )

    assert item.tags == (("manual", True), ("automatic", False))
    assert item.creators == ("Ada Lovelace",)
    assert item.is_seed is True


def test_normalize_note_and_annotation_are_not_recommendation_seeds() -> None:
    note = normalize_item(
        {
            "key": "NOTE0001",
            "version": 1,
            "data": {
                "itemType": "note",
                "parentItem": "PAPER001",
                "note": "<b>Private local note</b>",
            },
        }
    )
    annotation = normalize_item(
        {
            "key": "ANNO0001",
            "version": 1,
            "data": {
                "itemType": "annotation",
                "parentItem": "PAPER001",
                "annotationText": "quoted",
                "annotationComment": "comment",
            },
        }
    )

    assert note.is_seed is False
    assert note.note_text == "Private local note"
    assert annotation.is_seed is False
    assert annotation.annotation_text == "quoted"
    assert annotation.annotation_comment == "comment"
