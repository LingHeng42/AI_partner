"""把 sessions/ 下除 .gitkeep 之外的一切从 git 历史里删掉。

为什么不用 git filter-branch / git-filter-repo：
- filter-branch 依赖 MSYS sh.exe，在沙箱里建不了信号管道（Win32 error 5）
- pip 装 filter-repo 时写不了临时目录（Permission denied）
所以直接用 git 底层命令重建 tree/commit：
    git cat-file 读对象、git mktree 建新 tree、git commit-tree 建新 commit
不 spawn shell、不依赖第三方库。

用法：python tests/rewrite_history.py <ref> [...]
    例如 python tests/rewrite_history.py refs/heads/main
原提交不会丢失：它们仍在其它 ref 与 backup bundle 里（本脚本只动传入的 ref）。
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DROP_DIR = "sessions/"          # 这个目录下的内容一律从历史里删掉……
KEEP_PATH = "sessions/.gitkeep"  # ……除了这个占位文件


def git(*args, data=None, env=None):
    """跑一条 git 命令；data 非空时从 stdin 喂 bytes；命令失败直接抛错。"""
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {result.stderr.decode('utf-8', 'replace')}")
    return result.stdout


def should_drop(path: str) -> bool:
    return path.startswith(DROP_DIR) and path != KEEP_PATH


def tree_entries(tree_sha: str) -> list:
    """解析 tree 对象：返回 [(mode, name, child_sha)]。

    必须用 `cat-file tree`（原始字节），不能用 `-p`：
    -p 会把 tree 渲染成 `<mode> <type> <sha>\\t<name>` 的美化格式，没有 NUL 分隔符。
    """
    raw = git("cat-file", "tree", tree_sha)
    entries = []
    position = 0
    while position < len(raw):
        space = raw.index(b" ", position)
        nul = raw.index(b"\0", space)
        mode = raw[position:space].decode()
        name = raw[space + 1:nul].decode("utf-8", "replace")
        child = raw[nul + 1:nul + 21].hex()
        entries.append((mode, name, child))
        position = nul + 21
    return entries


def mktree(entries: list) -> str:
    payload = "".join(
        f"{mode} {'tree' if mode == '40000' else 'blob'} {child}\t{name}\n"
        for mode, name, child in entries
    )
    return git("mktree", data=payload.encode("utf-8")).decode().strip()


def main():
    refs = sys.argv[1:] or ["refs/heads/main"]
    tips = [git("rev-parse", r).decode().strip() for r in refs]
    commits = git("rev-list", "--topo-order", *tips).decode().split()

    # 1) 读出每个提交的 tree、parent、作者/提交者与提交信息。
    #    用 `cat-file commit`（原始对象字节）：`-p` 会把 GIT_* 环境变量也拼进去，
    #    很容易被当成提交信息的一部分（第一次就踩了这个坑）。
    info = {}
    for sha in commits:
        lines = git("cat-file", "commit", sha).decode("utf-8", "replace").split("\n")
        blank = lines.index("")
        def value(prefix):
            for line in lines[:blank]:
                if line.startswith(prefix):
                    return line.split(" ", 1)[1].strip()
            return ""
        info[sha] = {
            "tree": value("tree "),
            "parents": [l.split(" ", 1)[1].strip() for l in lines[:blank] if l.startswith("parent ")],
            "author": value("author "),
            "committer": value("committer "),
            "message": "\n".join(lines[blank + 1:]),
        }

    # 2) 重建 tree：递归到 sessions/ 时开始丢东西，其它路径原样保留
    cache = {}

    def rebuild(tree_sha: str, prefix: str = "") -> str:
        key = (tree_sha, prefix)
        if key in cache:
            return cache[key]
        kept = []
        for mode, name, child in tree_entries(tree_sha):
            path = f"{prefix}{name}"
            if should_drop(path):
                continue
            if mode == "40000":
                child = rebuild(child, path + "/")
            kept.append((mode, name, child))
        cache[key] = mktree(kept)
        return cache[key]

    # 3) 从旧到新重建提交：tree 换成过滤后的、parent 换成新哈希，
    #    作者/提交者身份与时间原样保留（用 GIT_* 环境变量传给 commit-tree）
    commit_map = {}
    for sha in reversed(commits):
        item = info[sha]
        new_tree = rebuild(item["tree"])
        args = ["commit-tree", new_tree]
        for parent in item["parents"]:
            args += ["-p", commit_map.get(parent, parent)]
        env = dict(os.environ)
        for who, var in ((item["author"], "AUTHOR"), (item["committer"], "COMMITTER")):
            # "名字 <邮箱> 时间戳 时区"
            if not who:
                continue
            name_email, _, when = who.rpartition("> ")
            name, _, email = name_email.rpartition(" <")
            env[f"GIT_{var}_NAME"] = name
            env[f"GIT_{var}_EMAIL"] = email
            if when:
                env[f"GIT_{var}_DATE"] = when
        new_sha = git(*args, data=(item["message"]).encode("utf-8"), env=env).decode().strip()
        commit_map[sha] = new_sha

    # 4) 只更新调用方指定的 ref
    for ref in refs:
        old = git("rev-parse", ref).decode().strip()
        git("update-ref", ref, commit_map[old])
        print(f"{ref}: {old[:8]} -> {commit_map[old][:8]}")
    print(f"共重写 {len(commit_map)} 个提交")


if __name__ == "__main__":
    main()
