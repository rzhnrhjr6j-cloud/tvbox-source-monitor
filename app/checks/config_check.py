"""L2 - configuration validity (spec §8).

The body is parsed with the same parser the discovery path uses, so a source
that is admitted is exactly a source that can be probed.
"""

from __future__ import annotations

from typing import Any

from ..parsers.json_parser import ConfigDoc, ParseError, parse_text, required_fields_present


def check_config(text: str) -> tuple[dict[str, Any], ConfigDoc | None]:
    try:
        doc = parse_text(text)
    except ParseError as exc:
        return (
            {
                "config_parse_success": False,
                "schema_detected": "",
                "error_code": exc.code,
                "error_message": exc.message[:500],
            },
            None,
        )

    complete, reason = required_fields_present(doc)
    fields: dict[str, Any] = {
        "config_parse_success": bool(complete),
        "schema_detected": doc.schema,
    }
    if not complete:
        fields["error_code"] = "INCOMPLETE_CONFIG"
        fields["error_message"] = reason
    return fields, doc
