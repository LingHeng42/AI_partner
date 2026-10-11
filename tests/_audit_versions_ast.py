"""用 AST 解析各 Streamlit 版本的真实函数签名，判断我们用的参数是否存在。

用法：python tests/_audit_versions_ast.py
"""
import ast
import urllib.request

BASE = "https://raw.githubusercontent.com/streamlit/streamlit/{tag}/lib/streamlit/"

# 我们要检查的 (API, 依赖的参数, 源码文件)
TARGETS = [
    ("button", ["width", "icon", "type", "help", "on_click", "key"], "elements/widgets/button.py"),
    ("form_submit_button", ["type", "icon", "width"], "delta_generator.py"),
    ("chat_input", ["submit_mode", "accept_file", "max_upload_size", "placeholder"], "elements/widgets/chat.py"),
    ("chat_message", ["avatar"], "elements/widgets/chat.py"),
    ("expander", ["type", "icon", "expanded", "key", "on_change"], "elements/layouts.py"),
    ("popover", ["width", "key", "help", "type"], "elements/layouts.py"),
    ("columns", ["vertical_alignment", "gap"], "elements/layouts.py"),
    ("form", ["border", "clear_on_submit", "enter_to_submit"], "delta_generator.py"),
    ("logo", ["size", "icon_image"], "commands/logo.py"),
    ("image", ["width", "caption"], "elements/image.py"),
]

VERSIONS = ["1.49.0", "1.52.0", "1.55.0", "1.58.0", "1.59.0", "1.61.0",
            "1.62.0", "1.63.0", "1.64.0", "1.65.0"]

cache: dict[str, str] = {}


def source(path: str, tag: str) -> str:
    key = f"{tag}:{path}"
    if key in cache:
        return cache[key]
    url = BASE.format(tag=tag) + path
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        text = ""
        print(f"  (取不到 {tag} {path}: {exc})")
    cache[key] = text
    return text


def params_of(text: str, func_name: str):
    """在文件里找 `def <func_name>(...)`（含方法定义），返回参数名集合。"""
    if not text:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            names = set()
            args = node.args
            for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                names.add(a.arg)
            if args.kwarg:
                names.add("**" + args.kwarg.arg)
            return names
    return None


print(f"{'API(参数)':34s} " + " ".join(f"{v.split('.')[1]:>4s}" for v in VERSIONS))
missing_summary = []
for api, params in [(t[0], t[1]) for t in TARGETS]:
    path = next(t[2] for t in TARGETS if t[0] == api)
    for param in params:
        row = []
        for tag in VERSIONS:
            text = source(path, tag)
            found = params_of(text, api)
            if found is None:
                row.append("  ?")
            elif param in found or f"**{param}" in found:
                row.append("   Y")
            elif "**kwargs" in found or any(p.startswith("**") for p in found):
                row.append("  ~Y")   # 有 **kwargs，参数会被吞掉（不报错）
            else:
                row.append("   .")
        label = f"{api}({param}=)"
        print(f"{label:34s} " + " ".join(row))
        if "." in row:
            first_ok = next((VERSIONS[i] for i, mark in enumerate(row) if "Y" in mark), None)
            missing_summary.append(f"{label} 最早支持: {first_ok}")

print()
print("Y=直接支持，~Y=有 **kwargs 会吞掉参数，.=不支持（会 TypeError/TypeError-like），?=取不到源码")
print()
for line in missing_summary:
    print(" ", line)
