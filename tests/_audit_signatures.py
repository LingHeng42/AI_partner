"""清单工具：打印代码里用到的 st.* API 的真实签名，判断参数是否受支持。"""
import inspect

import streamlit as st

print("streamlit 版本:", st.__version__)
print()

NAMES = [
    "button", "chat_input", "chat_message", "columns", "expander", "popover",
    "file_uploader", "form", "form_submit_button", "toggle", "checkbox",
    "slider", "number_input", "text_input", "text_area", "image", "logo",
    "markdown", "write_stream", "rerun", "stop", "set_page_config", "empty",
    "code", "caption", "subheader", "header", "title", "success", "warning",
    "error", "info", "divider",
]

for name in NAMES:
    fn = getattr(st, name, None)
    if fn is None:
        print(f"{name:18s} !! 不存在")
        continue
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        print(f"{name:18s} 无法取签名: {exc}")
        continue
    params = []
    for pname, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            params.append(f"**{pname}")
            continue
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            params.append(f"*{pname}")
            continue
        mark = ""
        if p.default is not inspect.Parameter.empty:
            default = p.default
            if callable(default):
                default = getattr(default, "__name__", "callable")
            mark = f"={default!r}"
        params.append(f"{pname}{mark}")
    joined = ", ".join(params)
    print(f"{name:18s} ({joined})")
