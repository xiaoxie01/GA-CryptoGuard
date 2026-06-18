from __future__ import annotations

import json


def build_markdown_card(content: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }


def build_markdown_card_json(content: str) -> str:
    return json.dumps(build_markdown_card(content), ensure_ascii=False)
