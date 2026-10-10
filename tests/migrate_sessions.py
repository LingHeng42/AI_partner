"""把旧位置（分区之前）的会话迁到"按用户分区"的新位置。

背景：加入多用户后，存档位置从

    sessions/<会话ID>/...

变成了

    users/<用户名>/sessions/<会话ID>/...      （文件后端）
    users/<用户名>/sessions/<会话ID>/...      （MongoDB 后端，key 字段）

所以旧数据"还在但读不到"。这个脚本把旧数据复制到当前用户名下（**默认不删除**，
确认没问题后再加 --delete 清掉旧位置）。

用法：
    # 本地文件后端（默认用户名 local）
    python tests/migrate_sessions.py --dry-run
    python tests/migrate_sessions.py
    python tests/migrate_sessions.py --delete      # 确认无误后清理旧副本

    # MongoDB 后端
    set MONGO_URI=mongodb+srv://...
    python tests/migrate_sessions.py --uid 你的用户名
"""

import argparse
import shutil
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))

from lingheng import config, storage  # noqa: E402

SKIP_NAMES = {"users", "drafts", ".gitkeep"}


def local_plan(uid: str) -> list:
    """文件后端：列出要搬的 (源目录, 目标目录)。"""
    root = Path(config.archive_dir())
    target_root = root / "users" / uid / "sessions"
    plan = []
    if not root.exists():
        return plan
    for entry in sorted(root.iterdir()):
        if entry.name in SKIP_NAMES or not entry.is_dir():
            continue
        # 只搬"像会话"的目录（有 meta.json）
        if not (entry / config.META_FILENAME).exists():
            continue
        plan.append((entry, target_root / entry.name))
    return plan


def local_migrate(uid: str, delete: bool, dry_run: bool) -> int:
    plan = local_plan(uid)
    if not plan:
        print("没有找到需要迁移的旧会话（可能已经迁过了）。")
        return 0
    for src, dst in plan:
        if dst.exists():
            print(f"跳过（目标已存在）：{src.name}")
            continue
        print(f"{'[预演] ' if dry_run else ''}{src.name}  ->  {dst}")
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        if delete:
            shutil.rmtree(src, ignore_errors=True)
            print(f"        已删除旧副本 {src}")
    if not dry_run and not delete:
        print("\n旧数据仍保留在原处。确认新位置读得到之后，可以加 --delete 再跑一次清理。")
    return len(plan)


def mongo_migrate(uid: str, delete: bool, dry_run: bool) -> int:
    """MongoDB 后端：旧数据在 key 里没有 users/ 前缀，给它补上。"""
    client = storage._client()
    collection = client[storage.MONGO_DB_NAME][storage.MONGO_COLLECTION]
    legacy = list(collection.find({"key": {"$not": {"$regex": "^users/"}}}))
    if not legacy:
        print("数据库里没有【未分区】的旧文档（可能已经迁过了）。")
        return 0
    print(f"找到 {len(legacy)} 条旧文档，准备把 key 加上 users/{uid}/ 前缀。")
    moved = 0
    for doc in legacy:
        old_key = doc["key"]
        # 旧形状：sessions/<会话ID>/...  或  sessions/<会话ID>.json
        new_key = f"users/{uid}/{old_key}"
        if new_key == old_key:
            continue
        print(f"{'[预演] ' if dry_run else ''}{old_key}  ->  {new_key}")
        if dry_run:
            continue
        collection.update_one({"_id": doc["_id"]},
                              {"$set": {"key": new_key, "uid": uid}})
        moved += 1
        if delete:
            collection.delete_one({"_id": doc["_id"]})
            print(f"        已删除旧文档 {old_key}")
    if not dry_run and not delete:
        print("\n旧文档仍保留。确认无误后可以加 --delete 再跑一次清理。")
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移旧会话到按用户分区的新位置")
    parser.add_argument("--uid", default=None, help="用户名（默认：local）")
    parser.add_argument("--delete", action="store_true", help="迁移后删除旧副本")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不改动")
    args = parser.parse_args()

    uid = args.uid or "local"
    print(f"当前后端：{storage.backend_kind()} | 目标用户：{uid}")
    if storage.backend_kind() == storage.MONGO_BACKEND:
        count = mongo_migrate(uid, args.delete, args.dry_run)
    else:
        count = local_migrate(uid, args.delete, args.dry_run)
    print(f"\n处理了 {count} 个旧会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
