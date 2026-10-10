"""secrets 格式与"只构造一次登录组件"的回归测试。

这三个坑都是真实踩过的，而且**在线上表现为"登录页完全不出现"**，排查时毫无线索：

1. ``AUTH_CREDENTIALS`` 写成**单行嵌套内联表**（``{ usernames = { a = {...}, b = {...} } }``）
   时，Streamlit 自带的 TOML 解析器会报 "Invalid inline table value encountered"，
   整个 secrets 文件都读不到 → ``auth_enabled()`` 变 False → 直接跳到"配置 API Key"。
   而 Python 内置的 ``tomllib`` 认为这种写法**合法**，所以本地"验证过没问题"也不保险。
   （单用户时 Streamlit 能解析，两个及以上用户就失败 —— 见下面的边界断言。）

2. ``st.secrets[...]`` 返回的是 Streamlit 的 ``AttrDict``（只读、且不一定是 ``dict``
   实例）。按普通 dict 处理会在两处翻车：
   - ``isinstance(raw, dict)`` 为 False → 被当成字符串去 json.loads → 静默变成空配置
   - 直接把它交给 streamlit-authenticator → 它 ``auto_hash=True`` 要就地改写密码
     → ``TypeError: Secrets does not support item assignment``

3. ``Authenticate()`` 内部创建 ``CookieManager(key="init")``，同一次脚本运行里
   构造两次就撞 ``StreamlitDuplicateElementKey: key='init'``（登录页直接报错）。

用法：python tests/test_secrets_format.py
"""
import os
import sys
import tomllib
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))
os.environ["AI_PARTNER_DISABLE_AUTH"] = "0"  # 本套件专门测登录，要开着门控

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 1. TOML 格式：多用户的嵌套内联表在 Streamlit 下不可用
# --------------------------------------------------------------------------- #
from streamlit.runtime.secrets import AttrDict, Secrets  # noqa: E402

PROBE_DIR = TESTS_DIR / ".tmp" / "secrets_format"
PROBE_DIR.mkdir(parents=True, exist_ok=True)

INLINE_ONE_USER = (
    'AUTH_CREDENTIALS = { usernames = { owner = { email = "a@b.c", '
    'name = "o", password = "pw" } } }\n'
)
INLINE_TWO_USERS = (
    'AUTH_CREDENTIALS = { usernames = { owner = { email = "a@b.c", '
    'name = "o", password = "pw1" }, friend1 = { email = "f@b.c", '
    'name = "f", password = "pw2" } } }\n'
)
SECTION_FORM = (
    "[AUTH_CREDENTIALS]\n\n"
    "[AUTH_CREDENTIALS.usernames.lingheng42]\n"
    'email = "Lingheng42@outlook.com"\n'
    'name = "lingheng42"\n'
    'password = "pw-for-test-1"\n\n'
    "[AUTH_CREDENTIALS.usernames.friend1]\n"
    'email = "friend1@example.com"\n'
    'name = "朋友1"\n'
    'password = "pw-for-test-2"\n'
)


def streamlit_parse(body: str):
    """用 Streamlit 自己的解析器读一段 TOML，返回 (成功?, 错误文本)。

    只在"是否抛异常"上下结论 —— Streamlit 的 Secrets 在默认文件缺失时读不出内容，
    所以成功时不去取解析结果。
    """
    path = PROBE_DIR / "probe.toml"
    path.write_text(body, encoding="utf-8")
    try:
        Secrets()._parse_file_path(str(path))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).split("file: ")[-1][:90]


ok_one, _ = streamlit_parse(INLINE_ONE_USER)
check("单用户的嵌套内联表 Streamlit 能解析", ok_one)

ok_two, err_two = streamlit_parse(INLINE_TWO_USERS)
check("两个用户的嵌套内联表会被 Streamlit 拒绝（所以必须用分节写法）", not ok_two, err_two)

ok_section, err_section = streamlit_parse(SECTION_FORM)
check("分节写法能被 Streamlit 解析", ok_section, err_section)

# 关键差异：内置 tomllib 认为两种写法都合法 —— 这就是"本地测不出来"的原因
for label, body in (("单用户", INLINE_ONE_USER), ("两用户", INLINE_TWO_USERS)):
    try:
        tomllib.loads(body)
        check(f"内置 tomllib 认为【{label}】的嵌套内联表合法", True)
    except Exception as exc:  # noqa: BLE001
        check(f"内置 tomllib 认为【{label}】的嵌套内联表合法", False, exc)

# 分节写法用 tomllib 解析出来的结构（真实库拿到的就是这个形状）
parsed_section = tomllib.loads(SECTION_FORM)
users = parsed_section["AUTH_CREDENTIALS"]["usernames"]
check("分节写法解析出全部用户", sorted(users) == ["friend1", "lingheng42"], sorted(users))
check("分节写法保留中文显示名", users["friend1"]["name"] == "朋友1", users["friend1"]["name"])
check("每个用户都有 password 字段", all("password" in u for u in users.values()), sorted(users))


# --------------------------------------------------------------------------- #
# 2. AttrDict：按"映射"处理，而不是按 dict（否则静默变空配置）
# --------------------------------------------------------------------------- #
from lingheng import auth  # noqa: E402

creds = AttrDict({
    "usernames": {
        "lingheng42": {"email": "a@b.c", "name": "lingheng42", "password": "pw"},
    }
})
auth._credentials_raw = lambda: creds
parsed = auth.credentials()
check("AttrDict 形态的凭据能被解析出来", bool(parsed.get("usernames")), parsed)
check("AttrDict 里的用户名读到了",
      list((parsed.get("usernames") or {}).keys()) == ["lingheng42"], parsed)
check("AttrDict 也能让 auth_enabled() 为 True", auth.auth_enabled() is True)

# _plain 必须产出可写的普通 dict（否则会被库的就地改密撞只读对象）
plain = auth._plain(creds)
check("_plain 产出普通 dict", type(plain) is dict, type(plain).__name__)
try:
    plain["usernames"]["lingheng42"]["password"] = "hashed"
    check("_plain 的结果可以就地改写（auto_hash 需要）", True)
except Exception as exc:  # noqa: BLE001
    check("_plain 的结果可以就地改写（auto_hash 需要）", False, exc)

# 其它形态：JSON 字符串 / 普通 dict / 空配置
auth._credentials_raw = lambda: '{"usernames": {"bob": {"password": "x"}}}'
check("JSON 字符串形态也能解析",
      list(auth.credentials().get("usernames", {})) == ["bob"], auth.credentials())

auth._credentials_raw = lambda: {"usernames": {"carol": {"password": "x"}}}
check("普通 dict 形态也能解析",
      list(auth.credentials().get("usernames", {})) == ["carol"], auth.credentials())

auth._credentials_raw = lambda: None
check("没有配置时凭据为空", auth.credentials() == {})
check("没有配置时 auth_enabled() 为 False", auth.auth_enabled() is False)


# --------------------------------------------------------------------------- #
# 3. 登录组件只构造一次（避免 CookieManager 的 key='init' 撞车）
# --------------------------------------------------------------------------- #
from streamlit.testing.v1 import AppTest  # noqa: E402

stub = PROBE_DIR / "double_build.py"
stub.write_text(
    "import sys\n"
    f"sys.path.insert(0, {str(TESTS_DIR.parent).replace(chr(92), '/')!r})\n"
    "import streamlit as st\n"
    "import streamlit_authenticator as stauth\n"
    "from lingheng import auth\n"
    "auth._credentials_raw = lambda: {'usernames': {'u': {'password': 'p'}}}\n"
    "calls = []\n"
    "class FakeAuthenticate:\n"
    "    def __init__(self, *a, **k):\n"
    "        calls.append(1)\n"
    "stauth.Authenticate = FakeAuthenticate\n"
    "a = auth._authenticator()\n"
    "b = auth._authenticator()\n"
    "st.session_state['_same'] = a is b\n"
    "st.session_state['_built'] = len(calls)\n",
    encoding="utf-8",
)
at = AppTest.from_file(str(stub), default_timeout=30).run()
check("同一次运行里两次取实例返回同一个对象", at.session_state.get("_same") is True,
      at.session_state.get("_same"))
check("登录组件只被构造了一次（避免 key='init' 撞车）",
      at.session_state.get("_built") == 1, at.session_state.get("_built"))
check("探针脚本本身没有异常", not at.exception, [e.message[:80] for e in at.exception])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
