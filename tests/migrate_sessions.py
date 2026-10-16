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


def clean_empty(uid: str, delete: bool, dry_run: bool) -> int:
    """清理"空壳会话"：没有任何消息、却还占着存档的会话。

    这些通常是早期 bug 写空的历史（或者是异常路径留下的垃圾）。它们已经**不会**
    出现在会话历史里（列表会过滤掉空会话），但存储里还留着。

    注意：已经变空的会话，内容找不回来了 —— 这个命令只是把空壳清掉。
    真要恢复内容，去 Atlas 的备份/快照里找。
    """
    from lingheng import sessions

    storage.set_current_uid(uid)
    empties = sessions.list_empty_sessions()
    if not empties:
        print("没有空壳会话。")
        return 0
    print(f"找到 {len(empties)} 个空壳会话：")
    for name in empties:
        print(f"  {name}")
        if dry_run:
            continue
        if delete:
            sessions.delete_session(name)
            print("        已删除")
    if not dry_run and not delete:
        print("\n空壳仍保留。确认无误后加 --empty --delete 再跑一次清理。")
    return len(empties)


def clean_drafts(uid: str, delete: bool, dry_run: bool, keep_days: float) -> int:
    """清理**过期草稿**（`sessions/drafts/*.draft`）。

    草稿是"还没产生对话的会话"的暂存（人设/参数/头像）。每条「新建会话」都会写
    一个；如果这条会话一直没发消息，它就不进会话历史，用户在界面上看不到、也删不掉
    它 —— 草稿于是永远留着，用几次就攒一堆（用户发现的：删了会话但 .draft 还在）。

    注意：**最近 keep_days 天内的草稿一律保留**。草稿里存着头像/人设，用户切走再
    切回来时那些设置必须还在（这是明确的功能）。只清理明显没人会再回去的。

    用法：
        python tests/migrate_sessions.py --drafts --dry-run
        python tests/migrate_sessions.py --drafts --delete
        python tests/migrate_sessions.py --drafts --delete --keep-days 7
    """
    from lingheng import sessions

    storage.set_current_uid(uid)
    ids = sessions.draft_ids()
    if not ids:
        print("没有草稿。")
        return 0
    print(f"当前用户 {uid} 下有 {len(ids)} 个草稿（保留最近 {keep_days} 天）：")
    removed = 0
    for name in ids:
        age = sessions._session_id_age_days(name)
        stale = age >= keep_days
        mark = "过期可清" if stale else "保留（还新）"
        print(f"  {name}  {age:.1f} 天  [{mark}]")
        if dry_run or not delete or not stale:
            continue
        sessions.discard_draft(name)
        removed += 1
    if dry_run:
        print("\n预演完成。确认无误后加 --delete 再跑一次。")
    elif not delete:
        print("\n未删除任何草稿。确认无误后加 --delete。")
    else:
        print(f"\n已清理 {removed} 个过期草稿。")
    return removed


def move_partition(source: str, target: str, delete: bool, dry_run: bool) -> int:
    """把一个"用户分区"下的数据搬到另一个分区。

    为什么需要：早期版本的 ui.main() 把"待办动作"排在设置用户名**之前**，而
    storage 里的当前用户名默认是 "local"，于是首次渲染时那批写入（新建会话、草稿）
    落进了 users/local/ —— 一个不属于任何人的分区。表现是"界面里有会话，但在自己
    账号下查不到，删除还报告 0 个键"。

    这个命令把 users/<source>/ 下的键改写成 users/<target>/（key 前缀替换，
    data 不动）。默认**只打印计划**，确认无误后加 --delete 才真正执行。

    用法：
        python tests/migrate_sessions.py --from-partition local --uid lingheng42 --dry-run
        python tests/migrate_sessions.py --from-partition local --uid lingheng42 --delete
    """
    if storage.backend_kind() != storage.MONGO_BACKEND:
        print("这个命令只对 MongoDB 后端有意义（本地文件后端的分区就是目录，直接搬目录即可）。")
        return 0
    collection = storage._client()[storage.MONGO_DB_NAME][storage.MONGO_COLLECTION]
    old_prefix = f"users/{source}/"
    new_prefix = f"users/{target}/"
    docs = list(collection.find({"uid": source, "key": {"$regex": "^" + old_prefix}}))
    if not docs:
        print(f"分区 {source} 下没有数据。")
        return 0
    print(f"分区 {source} -> {target}：找到 {len(docs)} 个文档")
    moved = 0
    for doc in docs:
        old_key = doc["key"]
        new_key = new_prefix + old_key[len(old_prefix):]
        if dry_run:
            print(f"  [预演] {old_key}\n         -> {new_key}")
            moved += 1
            continue
        # 目标位置若已存在同名键，**不覆盖**（那可能是新数据，不能拿旧的盖掉）
        if collection.count_documents({"uid": target, "key": new_key}, limit=1):
            print(f"  跳过（目标已存在）：{new_key}")
            continue
        collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"uid": target, "key": new_key}},
        )
        moved += 1
        if delete:
            pass  # 同一个文档直接改了 uid/key，不需要额外删除
    if dry_run:
        print(f"\n预演完成（{moved} 个）。确认无误后去掉 --dry-run 再跑一次。")
    else:
        print(f"\n已迁移 {moved} 个文档到 {target}。")
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移/清理存档")
    parser.add_argument("--uid", default=None,
                        help="用户名（**线上操作必须显式给**；只有本地自测才可以省略）")
    parser.add_argument("--delete", action="store_true",
                        help="迁移后删除旧副本 / 配合 --empty 真正删除空壳")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不改动")
    parser.add_argument("--empty", action="store_true",
                        help="改为清理空壳会话（没有任何消息的会话）")
    parser.add_argument("--drafts", action="store_true",
                        help="改为清理过期草稿（sessions/drafts/*.draft，白占空间的来源）")
    parser.add_argument("--keep-days", type=float, default=1.0, metavar="天数",
                        help="清理草稿时保留最近多少天（默认 1；草稿里存着头像/人设，"
                             "切走再切回还要用，别清太狠）")
    parser.add_argument("--from-partition", default=None, metavar="用户名",
                        help="把某个用户分区下的数据搬到 --uid 名下（修「写错分区」的历史数据）")
    args = parser.parse_args()

    uid = args.uid or "local"
    # 不给 --uid 就默默按 "local" 干活，正是"数据写进 users/local"那一类事故的
    # 帮凶：你以为在清自己的存档，其实在动一个不属于任何人的分区。连到线上数据库
    # 时直接拒绝，除非你明确写出 --uid。
    if not args.uid and storage.backend_kind() == storage.MONGO_BACKEND:
        print("拒绝执行：连着线上数据库时必须显式给 --uid（例如 --uid lingheng42）。\n"
              "不加 --uid 会默认操作 users/local —— 那是兜底分区，不是任何人的数据。")
        return 2
    print(f"当前后端：{storage.backend_kind()} | 目标用户：{uid}")
    if args.from_partition:
        count = move_partition(args.from_partition, uid, args.delete, args.dry_run)
        print(f"\n处理了 {count} 个文档。")
        return 0
    if args.empty:
        count = clean_empty(uid, args.delete, args.dry_run)
        print(f"\n处理了 {count} 个空壳会话。")
        return 0
    if args.drafts:
        count = clean_drafts(uid, args.delete, args.dry_run, args.keep_days)
        print(f"\n处理了 {count} 个过期草稿。")
        return 0
    if storage.backend_kind() == storage.MONGO_BACKEND:
        count = mongo_migrate(uid, args.delete, args.dry_run)
    else:
        count = local_migrate(uid, args.delete, args.dry_run)
    print(f"\n处理了 {count} 个旧会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
