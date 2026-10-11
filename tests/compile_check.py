"""严格编译检查：任何语法错误都要报出来（不要靠肉眼）。

用法：python tests/compile_check.py
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
targets = sorted((ROOT / "lingheng").glob("*.py")) + [
    ROOT / "AI_partner.py",
    *(sorted((ROOT / "tests").glob("*.py"))),
]

bad = []
for path in targets:
    try:
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:  # noqa: BLE001
        bad.append((path.name, str(exc).split("\n")[0][:120]))

for name, err in bad:
    print(f"FAIL  {name}: {err}")
print(f"\n检查了 {len(targets)} 个文件，语法错误 {len(bad)} 个")
sys.exit(1 if bad else 0)
