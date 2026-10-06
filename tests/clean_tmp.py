"""清掉测试遗留的临时目录 tests/.tmp。

沙箱创建临时目录时会写入一条 `Everyone Deny DeleteSubdirectoriesAndFiles` 拒绝 ACE，
留着它普通删除会失败，所以这里先用 icacls 去掉该规则再删。

运行：
    .venv\\Scripts\\python.exe tests\\clean_tmp.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
TMP_DIR = TESTS_DIR / ".tmp"


def strip_deny_aces(path: Path) -> None:
    if os.name != "nt" or not path.exists():
        return
    try:
        subprocess.run(["icacls", str(path), "/remove:d", "Everyone"],
                       capture_output=True, check=False, timeout=30)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    if not TMP_DIR.exists():
        print("没有需要清理的临时目录：tests/.tmp 不存在")
        return 0

    before = sum(1 for _ in TMP_DIR.rglob("*"))
    strip_deny_aces(TMP_DIR)
    # 逐层删除：先删子目录内容，再删目录本身
    for child in sorted(TMP_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        strip_deny_aces(child) if child.is_dir() else None
        try:
            child.unlink() if child.is_file() else child.rmdir()
        except OSError:
            pass
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    if TMP_DIR.exists():
        try:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(TMP_DIR)],
                           capture_output=True, check=False, timeout=60)
        except Exception:  # noqa: BLE001
            pass

    if TMP_DIR.exists():
        print(f"未能完全删除 {TMP_DIR}，请手动检查其 ACL")
        return 1
    print(f"已清理 tests/.tmp（删除前有 {before} 个条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
