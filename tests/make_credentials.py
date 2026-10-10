"""生成可粘贴进 secrets 的用户表（密码用 bcrypt 哈希）。

为什么需要它：streamlit-authenticator 的用户表里 password 建议放哈希，
手写不现实。这个脚本把用户名/密码转成一行可直接复制的 TOML。

用法：
    # 直接给明文，脚本会哈希
    python tests/make_credentials.py owner:我的密码 friend1:朋友的密码

    # 哈希需要 bcrypt（streamlit-authenticator 的依赖，装它就有）
    .venv\\Scripts\\python.exe -m pip install streamlit-authenticator

输出示例（把最后一行整体复制到 secrets 的 AUTH_CREDENTIALS = 后面）：
    { usernames = { owner = { email = "", name = "owner", password = "$2b$12$..." } } }
"""

import sys


def hasher():
    """拿到 bcrypt 的哈希函数。streamlit-authenticator 内部也是用它。"""
    try:
        import bcrypt
    except ImportError:
        print("缺少 bcrypt。请先安装：.venv\\Scripts\\python.exe -m pip install streamlit-authenticator")
        sys.exit(2)

    def _hash(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    return _hash


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 2
    hash_password = hasher()
    entries = []
    for item in argv:
        if ":" not in item:
            print(f"格式应为 用户名:密码，收到的是 {item!r}")
            return 2
        username, password = item.split(":", 1)
        username = username.strip()
        if not username or not password:
            print(f"用户名或密码为空：{item!r}")
            return 2
        entries.append(
            f'{username} = {{ email = "", name = "{username}", '
            f'password = "{hash_password(password)}" }}'
        )
    print()
    print("把下面这一行整体复制到 secrets 里的 AUTH_CREDENTIALS = 后面：")
    print()
    print("{ usernames = { " + ", ".join(entries) + " } }")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
