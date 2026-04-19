"""Validate CLAUDE.draft.md JSON block parses + extract keys."""
import json
import re
import sys

text = open("CLAUDE.draft.md", encoding="utf-8").read()
m = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
if not m:
    sys.exit("no ```json fence found")
try:
    data = json.loads(m.group(1))
except json.JSONDecodeError as e:
    sys.exit(f"JSON parse error at line {e.lineno} col {e.colno}: {e.msg}")

print("JSON: valid")
print("Top-level keys:", len(data))
for k, v in data.items():
    if isinstance(v, list):
        print(f"  {k}: list[{len(v)}]")
    elif isinstance(v, dict):
        print(f"  {k}: dict[{len(v)}]")
    else:
        print(f"  {k}: scalar")
