"""清单工具：列出代码里用到的所有 st.* API 及其调用位置。"""
import re
from pathlib import Path

root = Path(".")
targets = [root / "AI_partner.py"] + sorted((root / "lingheng").glob("*.py"))
pat = re.compile(r"\bst\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
found = {}
for f in targets:
    text = f.read_text(encoding="utf-8")
    for i, line in enumerate(text.split("\n"), 1):
        for m in pat.finditer(line):
            found.setdefault(m.group(1), []).append(f"{f.name}:{i}")

print(f"共用到 {len(found)} 个 st.* API")
print()
for name in sorted(found):
    locs = found[name]
    print(f"{name:22s} x{len(locs):<3d} " + ", ".join(locs[:5]))
