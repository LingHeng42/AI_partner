"""探测各 Streamlit 版本是否支持我们用到的参数（从 GitHub 原始文件判断）。

用法：python tests/_audit_versions.py
"""
import re
import urllib.request

BASE = "https://raw.githubusercontent.com/streamlit/streamlit/{tag}/lib/streamlit/"

# (说明, 文件路径, 正则)  —— 命中说明该版本支持
CHECKS = [
    ("button(width=)", "elements/widgets/button.py", r"def button\([\s\S]{0,1200}?width:\s*Width"),
    ("button(icon=)", "elements/widgets/button.py", r"def button\([\s\S]{0,900}?icon:\s*str\s*\|\s*None"),
    ("chat_input(submit_mode=)", "elements/widgets/chat.py", r"submit_mode"),
    ("chat_input(accept_file=)", "elements/widgets/chat.py", r"accept_file"),
    ("expander(type=)", "elements/layouts.py", r"def expander\([\s\S]{0,800}?type:\s*Literal\["),
    ("expander(icon=)", "elements/layouts.py", r"def expander\([\s\S]{0,800}?icon:\s*str\s*\|\s*None"),
    ("popover(width=)", "elements/layouts.py", r"def popover\([\s\S]{0,900}?width:"),
    ("columns(vertical_alignment=)", "elements/layouts.py", r"vertical_alignment"),
    ("form(border=)", "elements/layouts.py", r"def form\([\s\S]{0,400}?border"),
]

VERSIONS = ["1.49.0", "1.50.0", "1.51.0", "1.52.0", "1.53.0", "1.54.0",
            "1.55.0", "1.56.0", "1.57.0", "1.58.0", "1.59.0", "1.60.0",
            "1.61.0", "1.62.0", "1.63.0", "1.64.0", "1.65.0"]

cache: dict[str, str] = {}


def fetch(path: str, tag: str) -> str:
    key = f"{tag}:{path}"
    if key in cache:
        return cache[key]
    url = BASE.format(tag=tag) + path
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        text = f"__ERR__ {exc}"
    cache[key] = text
    return text


print(f"{'检查项':30s} " + " ".join(f"{v.split('.')[1]:>4s}" for v in VERSIONS))
for label, path, pattern in CHECKS:
    row = []
    for tag in VERSIONS:
        text = fetch(path, tag)
        if text.startswith("__ERR__"):
            row.append("  ?")
            continue
        row.append("   Y" if re.search(pattern, text) else "   .")
    print(f"{label:30s} " + " ".join(row))

print()
print("Y = 该版本支持，. = 不支持，? = 取不到文件")
