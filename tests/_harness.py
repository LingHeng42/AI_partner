"""测试公共环境：临时目录、环境变量、清理。

三条规矩（都来自踩过的坑）：
1. 存档目录一律用 AI_PARTNER_SESSIONS_DIR 指到 tests/.tmp 下的隔离目录——
   测试永远不碰真实的 sessions/。
2. 所有临时产物都放在同一个 tests/.tmp 里，不再按 pid 建一堆目录。
3. 每次运行开头先清空 tests/.tmp，所以磁盘上任何时候最多只有一份临时目录，
   不会越攒越多。

为什么清理放在"开头"而不是进程退出时：
- atexit 不可靠：解释器退出时还有别的钩子会再往该目录写东西，跑完仍有残留；
- Windows 上 os.kill(pid, 0) 对已退出的进程不报错，做不出"看守进程"来兜底。
需要立刻清掉时用：`python tests/clean_tmp.py`
（沙箱创建的目录带 `Everyone Deny DeleteSubdirectoriesAndFiles` 拒绝 ACE，
  普通删除会失败，所以要先用 icacls 去掉这条规则。）

用法：测试文件开头 import 本模块即可，路径与环境变量都已就绪。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT = TESTS_DIR.parent
TMP_DIR = TESTS_DIR / ".tmp"
# 每次运行用独立的存档目录名：沙箱往 .tmp 里写的临时子目录带
# `Everyone Deny DeleteSubdirectoriesAndFiles` 拒绝 ACE，在受限权限下连 icacls 都
# 被拒（Access is denied），那层目录删不掉。用独立名字就不用依赖删除成功——
# 旧目录即使残留也不会影响本次运行。
SESSIONS_DIR = TMP_DIR / f"sessions_{os.getpid()}"

# 应用实现拆到了 lingheng/ 包里，入口 AI_partner.py 只剩代理导出。
# 那些"读源码做静态断言"的测试要用下面两个函数拿源码，别再去读入口文件。
APP_PACKAGE = PROJECT / "lingheng"


def app_source() -> str:
    """整个应用的源码（各模块拼接），用于源码级断言。"""
    parts = []
    for path in sorted(APP_PACKAGE.glob("*.py")):
        parts.append(f"# ===== {path.name} =====\n{path.read_text(encoding='utf-8')}")
    return "\n".join(parts)


def ui_source() -> str:
    """页面渲染那一层（侧边栏与主区域）的源码。"""
    return (APP_PACKAGE / "ui.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 存档的真实路径
#
# 存档现在按用户分区（users/<用户名>/sessions/...），用户名由 storage 决定，
# 测试里的当前用户是 "local"。测试要断言"磁盘上真的有这个文件"时，用下面的
# archive_path() 把存储键名换成真实路径，而不是自己去拼目录结构。
# --------------------------------------------------------------------------- #
LOCAL_UID = "local"


def archive_path(rel: str) -> Path:
    """存储键名 → 隔离存档目录下的真实路径。

    rel 可以是完整键名（"sessions/<会话ID>/meta.json"），也可以只给会话 ID。
    以 "sessions" 结尾的输入按"整个 sessions 根"处理（不会重复拼一层）。
    """
    text = str(rel).replace("\\", "/").strip("/")
    if text == "sessions":
        text = ""
    elif text and not text.startswith("sessions/"):
        text = f"sessions/{text}"
    base = SESSIONS_DIR / "users" / LOCAL_UID
    return base / text if text else base / "sessions"


def session_meta_path(session_id: str) -> Path:
    return archive_path(f"sessions/{session_id}/meta.json")


def session_branch_path(session_id: str, branch_id: str = "main") -> Path:
    return archive_path(f"sessions/{session_id}/branches/{branch_id}.json")


def session_dir(session_id: str) -> Path:
    return archive_path(f"sessions/{session_id}")


def drafts_dir() -> Path:
    return archive_path("sessions/drafts")


def legacy_flat_path(session_id: str) -> Path:
    """旧扁平存档（sessions/<会话ID>.json）的路径。"""
    return archive_path(f"sessions/{session_id}.json")


def user_root() -> Path:
    """当前（local）用户的存档根目录。"""
    return SESSIONS_DIR / "users" / LOCAL_UID


def sessions_root() -> Path:
    """当前（local）用户的 sessions 目录。"""
    return user_root() / "sessions"


def seed_user_root() -> None:
    """建好用户的存档根目录，供需要在 App 运行前写文件的测试用。"""
    user_root().mkdir(parents=True, exist_ok=True)


def real_png(pixel=(255, 0, 0)) -> bytes:
    """生成一张 1×1 的真实合法 PNG。

    测试里不能用"魔数 + 随便几个字节"的假图片：应用会用 Pillow 校验头像，
    假图片会被正确判定为坏图（那条兜底逻辑本身也有测试覆盖）。
    """
    import struct as _struct

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (_struct.pack(">I", len(payload)) + tag + payload
                + _struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = _struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1×1, 8bit truecolor
    raw = b"\x00" + bytes(pixel)  # 每行前面一个 filter 字节
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _run(cmd: list) -> None:
    try:
        subprocess.run(cmd, capture_output=True, check=False, timeout=60)
    except Exception:  # noqa: BLE001 - 清理失败不该影响测试
        pass


def strip_deny_aces(path: Path) -> None:
    """去掉目录上沙箱留下的拒绝删除 ACE（Windows）。"""
    if os.name == "nt" and path.exists():
        _run(["icacls", str(path), "/remove:d", "Everyone"])


def take_ownership(path: Path) -> None:
    """把目录及子项的所有权收归当前用户（沙箱建的临时目录属于其它主体）。"""
    if os.name == "nt" and path.exists():
        _run(["takeown", "/f", str(path), "/r", "/d", "y"])
        _run(["icacls", str(path), "/grant", f"{os.environ.get('USERNAME', '')}:(OI)(CI)F",
              "/t", "/c"])


def purge(path: Path) -> bool:
    """尽力彻底删除 path（先处理拒绝 ACL 与所有权），返回是否已不存在。"""
    if not path.exists():
        return True
    if path.is_dir():
        try:
            for child in path.iterdir():
                strip_deny_aces(child)
        except OSError:
            pass
    strip_deny_aces(path)
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        take_ownership(path)
        shutil.rmtree(path, ignore_errors=True)
    if path.exists() and os.name == "nt":
        _run(["cmd", "/c", "rmdir", "/s", "/q", str(path)])
    return not path.exists()


def reset_tmp() -> None:
    """清空并重建临时目录（每次运行都从干净状态开始）。

    删不掉的（沙箱建的受限 ACL 目录）会被忽略，并顺手清理上一轮留下的
    sessions_* 隔离目录——即使删不掉也无所谓，因为本次用的是独立目录名。
    """
    if TMP_DIR.exists():
        # 上一轮的隔离存档目录：能删就删，删不掉也不影响本次运行
        for stale in TMP_DIR.glob("sessions_*"):
            purge(stale)
        try:
            for child in TMP_DIR.iterdir():
                strip_deny_aces(child)
        except OSError:
            pass
    purge(TMP_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSIONS_DIR.is_dir():
        sys.exit(f"无法创建测试存档目录：{SESSIONS_DIR}")
    # 隔离目录必须是干净的：有任何残留都直接失败，避免跑出假结果
    stale = [p.name for p in SESSIONS_DIR.iterdir()]
    if stale:
        sys.exit(f"测试存档目录不干净（{SESSIONS_DIR}）：{stale}")
    # 建好"local"用户的根目录：存档按用户分区放在 users/<用户名>/ 下
    (SESSIONS_DIR / "users" / LOCAL_UID / "sessions").mkdir(parents=True, exist_ok=True)
    # 护栏：绝不允许测试去动真实的 sessions/（曾经因为测试指向它而误删用户存档）
    real_sessions = (PROJECT / "sessions").resolve()
    if SESSIONS_DIR.resolve() == real_sessions or real_sessions in SESSIONS_DIR.resolve().parents:
        sys.exit(f"测试存档目录指向了真实目录，拒绝运行：{SESSIONS_DIR}")


# 干净起点 + 环境变量 + 路径
reset_tmp()
sys.dont_write_bytecode = True

# 解释器退出时 tempfile 会去删自己登记过的临时目录；沙箱建的目录带
# "Everyone 拒绝删除" ACL，删不掉就抛 PermissionError，把进程退出码弄成 1
# （断言全过却"失败"）。这里既卸掉模块级清理钩子，也摘掉每个 TemporaryDirectory
# 自己的终结器（它不经过 _cleanup，会走临时目录的 finalizer）。
try:
    import tempfile as _tempfile

    _tempfile._cleanup = lambda *a, **k: None

    class _NoCleanupTemporaryDirectory(_tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # 摘掉"退出时删除自己"的终结器，避免删不掉时抛异常
            self._finalizer.detach()

    _tempfile.TemporaryDirectory = _NoCleanupTemporaryDirectory
except Exception:  # noqa: BLE001
    pass

os.environ["TMPDIR"] = str(TMP_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-used"
# 关键：把应用的存档目录隔离到临时目录
os.environ["AI_PARTNER_SESSIONS_DIR"] = str(SESSIONS_DIR)
# 让 tempfile 也把东西放进 TMP_DIR，别在系统临时目录里留下受限 ACL 的残留
tempfile.tempdir = str(TMP_DIR)

for _path in (str(PROJECT), str(TMP_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

