"""MongoDB 配置自检：本地跑一次，就知道线上到底卡在哪一步。

为什么需要它：本地 `pymongo.MongoClient(uri).ping()` 成功只能证明**你这台电脑**
连得上；Streamlit Cloud 的服务器是另一台机器，可能因为 Network Access 白名单、
secrets 没读到等原因连不上。这个脚本把这几件事分开检查并给出结论。

用法：
    # 方式一：直接给连接串（只在本机内存里用，不写任何文件）
    python tests/check_mongo.py "mongodb+srv://用户:密码@集群.mongodb.net/?appName=x"

    # 方式二：读本地 .streamlit/secrets.toml 里的 MONGO_URI
    python tests/check_mongo.py

    # 只看某个用户名的数据（默认 local）
    python tests/check_mongo.py "连接串" --uid 你的用户名
"""

import argparse
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))

MONGO_DB = "lingheng"
MONGO_COLLECTION = "files"


def mask(uri: str) -> str:
    import re

    return re.sub(r"://([^:/@]+):([^@/]+)@", r"://\1:***@", uri or "")


def read_uri_from_secrets() -> str:
    """从 .streamlit/secrets.toml 里读 MONGO_URI（用 Streamlit 自己的解析器）。"""
    path = TESTS_DIR.parent / ".streamlit" / "secrets.toml"
    if not path.exists():
        print(f"✗ 找不到 {path}")
        return ""
    try:
        from streamlit.runtime.secrets import Secrets

        secrets = Secrets()
        secrets._parse_file_path(str(path))
        keys = list(secrets.keys())
        print(f"✓ secrets.toml 能解析，顶层键：{sorted(keys)}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ secrets.toml 解析失败：{type(exc).__name__}: {exc}")
        print("  这会让整个 secrets 文件读不到（包括 DEEPSEEK_API_KEY 和 MONGO_URI）。")
        print("  最常见原因：用户表写成了单行嵌套内联表，或少了引号/逗号。")
        return ""
    # 解析成功后用 tomllib 取值（Secrets 在文件缺失时读不出内容）
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    uri = str(data.get("MONGO_URI") or "").strip()
    if uri:
        print(f"✓ secrets.toml 里有 MONGO_URI：{mask(uri)}")
    else:
        print("✗ secrets.toml 里没有 MONGO_URI")
    return uri


def check(uri: str, uid: str) -> int:
    try:
        import pymongo
    except ImportError:
        print("✗ 没装 pymongo：.venv\\Scripts\\python.exe -m pip install pymongo")
        return 2

    print(f"\n连接串：{mask(uri)}")
    print(f"pymongo：{pymongo.version}")

    # ① 能不能连上
    try:
        client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        print("✓ 步骤① 连接成功")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 步骤① 连接失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("\n常见原因（按概率排序）：")
        print("  1. Atlas → Network Access 没放行你这台机器当前的出口 IP")
        print("     （手机热点/公司网络会变，之前放行的可能已经失效）")
        print("  2. 用户名或密码不对；密码里的 @ : / ? # 等字符没做 URL 编码")
        print("  3. 集群已暂停（免费集群闲置会暂停）或被删除")
        print("  4. 连接串少了 ?retryWrites=true&w=majority 之类参数（一般不影响）")
        return 1

    # ② 看得到哪些库
    try:
        names = client.list_database_names()
        print(f"✓ 步骤② 可见数据库：{names}")
        if MONGO_DB not in names:
            print(f"  （还没有 `{MONGO_DB}` 库 —— 说明**从来没有成功写入过**）")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 步骤② 列数据库失败：{type(exc).__name__}: {str(exc)[:160]}")
        return 1

    # ③ 这个用户名下有没有数据
    collection = client[MONGO_DB][MONGO_COLLECTION]
    total = collection.count_documents({})
    mine = collection.count_documents({"uid": uid})
    uids = sorted(collection.distinct("uid"))
    print(f"✓ 步骤③ `{MONGO_DB}.{MONGO_COLLECTION}` 共 {total} 个文档；"
          f"uid={uid!r} 的有 {mine} 个")
    if uids:
        print(f"  库里出现过的 uid：{uids}")
    if total == 0:
        print("\n→ 结论：数据库里一条记录都没有。应用侧从来没写进来过，")
        print("  说明线上**没在用这个数据库**（多半是 MONGO_URI 没配到 Secrets，")
        print("  或者 secrets 文件解析失败）。请到 App 的 Manage app → Logs 搜 `[storage]`。")
        return 1

    session_ids = sorted({
        doc["key"].split("/")[3]
        for doc in collection.find({"uid": uid, "key": {"$regex": "/sessions/[^/]+/meta.json$"}})
        if doc["key"].startswith("users/")
    })
    print(f"  这个 uid 下的会话：{session_ids or '（无）'}")
    if not mine:
        print(f"\n→ 数据库里有数据，但没有 uid={uid!r} 的：")
        print("  说明线上写入时用的用户名和你现在查的不一样。")
        print("  用户名取自登录账号（secrets 里用户表的键名），确认一下拼写。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MongoDB 配置自检")
    parser.add_argument("uri", nargs="?", default="", help="连接串（省略则读 secrets.toml）")
    parser.add_argument("--uid", default="local", help="用户名（默认 local）")
    args = parser.parse_args()

    uri = args.uri.strip() or read_uri_from_secrets()
    if not uri:
        print("\n没拿到连接串。可以这样用：")
        print('  python tests/check_mongo.py "mongodb+srv://..."')
        return 2
    return check(uri, args.uid)


if __name__ == "__main__":
    sys.exit(main())
