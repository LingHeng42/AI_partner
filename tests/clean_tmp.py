"""清掉测试遗留的临时目录 tests/.tmp。

沙箱创建临时目录时会写入一条 `Everyone Deny DeleteSubdirectoriesAndFiles` 拒绝 ACE，
且目录属于其它主体；在受限权限下连 icacls 都会被拒（Access is denied）。所以：
- 普通权限下能删的都删掉；
- 删不掉的会明确报告，需要以管理员身份重跑本脚本。

（真正防堆积的手段在 _harness.py：每次测试开头都会清空 tests/.tmp，
  文件系统里任何时候最多只有一份临时目录。）

运行：
    .venv/Scripts/python.exe tests/clean_tmp.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import TMP_DIR, strip_deny_aces  # noqa: E402


def _run(cmd):
    try:
        subprocess.run(cmd, capture_output=True, check=False, timeout=60)
    except Exception:  # noqa: BLE001
        pass


def force_delete(path):
    """先去掉拒绝 ACE，失败就取回所有权，最后兜底用 rmdir。"""
    strip_deny_aces(path)
    shutil.rmtree(path, ignore_errors=True)
    if path.exists() and os.name == "nt":
        _run(["takeown", "/f", str(path), "/r", "/d", "y"])
        _run(["icacls", str(path), "/grant",
              f"{os.environ.get('USERNAME', '')}:(OI)(CI)F", "/t", "/c"])
        shutil.rmtree(path, ignore_errors=True)
    if path.exists() and os.name == "nt":
        _run(["cmd", "/c", "rmdir", "/s", "/q", str(path)])


if __name__ == "__main__":
    if not TMP_DIR.exists():
        print("没有需要清理的临时目录：tests/.tmp 不存在")
        sys.exit(0)

    before = sum(1 for _ in TMP_DIR.rglob("*"))
    force_delete(TMP_DIR)

    if not TMP_DIR.exists():
        print(f"已清理 tests/.tmp（删除前有 {before} 个条目）")
        sys.exit(0)

    left = [p.name for p in TMP_DIR.iterdir()]
    print(f"tests/.tmp 仍有 {len(left)} 个条目无法在受限权限下删除：{left}")
    print("它们带有沙箱写入的拒绝 ACL，请以管理员身份重跑：")
    print("    .venv/Scripts/python.exe tests/clean_tmp.py")
    sys.exit(1)