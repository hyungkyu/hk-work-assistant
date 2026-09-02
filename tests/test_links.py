from rlwrld_worklog.links import canonical_notion_page_id, extract_notion_urls


def test_extracts_notion_links_from_nested_values() -> None:
    value = {
        "text": "read https://www.notion.so/Weekly-0123456789abcdef0123456789abcdef).",
        "attachments": [{"url": "https://example.com"}],
    }
    assert extract_notion_urls(value) == [
        "https://www.notion.so/Weekly-0123456789abcdef0123456789abcdef"
    ]
    assert canonical_notion_page_id(extract_notion_urls(value)[0]) == (
        "01234567-89ab-cdef-0123-456789abcdef"
    )

