#!/usr/bin/env python3
"""Rebuild urban_group_visualiser.html from urban_group_comparison.json.

Run build_urban_group_comparison.py first to regenerate the JSON, then this
script to bake it into the self-contained viewer page.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
JSON_PATH = HERE / "urban_group_comparison.json"
HTML_PATH = HERE / "urban_group_visualiser.html"


def main() -> None:
    rows = json.loads(JSON_PATH.read_text())

    tpl = HTML_PATH.read_text()
    out, n = re.subn(
        r"const DATA = .*?;\n",
        "const DATA = " + json.dumps(rows) + ";\n",
        tpl, count=1, flags=re.S,
    )
    if n != 1:
        sys.exit(f"Could not find the 'const DATA = ...;' line in {HTML_PATH}")
    HTML_PATH.write_text(out)
    print(f"Rebuilt {HTML_PATH.name} with {len(rows)} constituencies from {JSON_PATH.name}")


if __name__ == "__main__":
    main()
