"""The build safety valve (spec §18, §19, §30, §32).

This is the single most important guarantee in the project: a bad detection
run must never empty the published tvbox.json.
"""

from __future__ import annotations

from app.build.validator import count_items, validate_output


def item(name: str, url: str, **extra) -> dict:
    return {"name": name, "url": url, **extra}


HEALTHY = {"urls": [item(f"S{i}", f"https://s{i}.example.com/c.json") for i in range(3)]}


def test_healthy_output_passes():
    result = validate_output(HEALTHY, min_sources=3)
    assert result.ok is True
    assert result.new_count == 3
    assert result.reasons == []


def test_empty_output_is_blocked():
    result = validate_output({"urls": []}, min_sources=3)
    assert result.ok is False
    assert "NO_ITEMS" in result.reasons
    assert any(reason.startswith("BELOW_MIN_SOURCES") for reason in result.reasons)


def test_below_min_sources_is_blocked():
    result = validate_output({"urls": [item("S1", "https://s1.example.com/c.json")]}, min_sources=3)
    assert result.ok is False
    assert result.reasons == ["BELOW_MIN_SOURCES:1<3"]


def test_missing_name_is_blocked():
    result = validate_output({"urls": [{"url": "https://a.example.com/c.json"} for _ in range(3)]})
    assert result.ok is False
    assert "ITEM_0_MISSING_NAME" in result.reasons


def test_non_http_url_is_blocked():
    result = validate_output({"urls": [item(f"S{i}", "ftp://a.example.com/c.json") for i in range(3)]})
    assert result.ok is False
    assert "ITEM_0_NOT_HTTP" in result.reasons


def test_duplicate_urls_are_blocked_even_when_spelled_differently():
    urls = [
        item("S1", "https://a.example.com/c.json"),
        item("S2", "https://A.example.com/c.json/"),
        item("S3", "https://b.example.com/c.json"),
    ]
    result = validate_output({"urls": urls})
    assert result.ok is False
    assert any(reason.startswith("DUPLICATE_URL") for reason in result.reasons)


def test_massive_drop_against_the_published_file_is_blocked():
    previous = {"urls": [item(f"S{i}", f"https://s{i}.example.com/c.json") for i in range(10)]}
    new = {"urls": [item("S1", "https://s1.example.com/c.json"), item("S2", "https://s2.example.com/c.json")]}
    result = validate_output(new, previous=previous, min_sources=2, max_drop_ratio=0.70)
    assert result.ok is False
    assert result.previous_count == 10
    assert any(reason.startswith("ACTIVE_DROP_") for reason in result.reasons)


def test_normal_shrink_is_allowed():
    previous = {"urls": [item(f"S{i}", f"https://s{i}.example.com/c.json") for i in range(10)]}
    new = {"urls": [item(f"S{i}", f"https://s{i}.example.com/c.json") for i in range(8)]}
    result = validate_output(new, previous=previous, min_sources=3, max_drop_ratio=0.70)
    assert result.ok is True


def test_growth_from_empty_previous_is_allowed():
    result = validate_output(HEALTHY, previous={"urls": []}, min_sources=3)
    assert result.ok is True


def test_count_items_counts_only_dict_entries():
    """Items are dicts with name/url.  A bare string list is not a valid
    published entry, so it must not be counted as one (that would let a
    degenerate output look healthy)."""
    items = [{"name": "A", "url": "https://a.example.com/c.json"}] * 2
    assert count_items({"urls": items}) == 2
    assert count_items({"sites": items}) == 1 + 1
    assert count_items(items) == 2
    assert count_items({"urls": ["https://a.example.com/c.json"]}) == 0
    assert count_items({"urls": [1, 2]}) == 0
    assert count_items({"nope": 1}) == 0
