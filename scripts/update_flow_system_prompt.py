#!/usr/bin/env python3
"""Synchronize an Agent system prompt from a versioned text file.

Langflow exports prompt values inside flow JSON. Keeping the readable prompt in
``flows/components`` makes review possible while this utility ensures the
bundled graph contains the exact same text used by repository-owned migration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def update_agent_prompt(flow: dict[str, Any], prompt: str) -> None:
    matches = [
        node
        for node in flow.get("data", {}).get("nodes", [])
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Agent node, found {len(matches)}")
    template = matches[0]["data"]["node"]["template"]
    template["system_prompt"]["value"] = prompt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()

    flow = json.loads(args.flow_file.read_text(encoding="utf-8"))
    prompt = args.prompt_file.read_text(encoding="utf-8").rstrip("\n")
    update_agent_prompt(flow, prompt)
    args.flow_file.write_text(
        json.dumps(flow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
