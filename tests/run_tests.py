"""按顺序在各自独立的进程里运行所有测试套件。

为什么每个套件要单独起进程：AppTest 的脚本运行器会复用 sys.modules 里已缓存的
AI_partner —— 同一个进程里只有第一次运行会真正执行应用脚本，之后的套件会拿到
上一次的模块状态，断言就失去意义（表现为"单独跑通过、连着跑失败"）。

运行：
    .venv\\Scripts\\python.exe tests\\run_tests.py
"""

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SUITES = ("test_logic.py", "test_smoke.py", "test_row_render.py", "test_branch_ui.py")

failures = []
for suite in SUITES:
    print(f"===== {suite} =====")
    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / suite)],
        cwd=str(TESTS_DIR.parent),
    )
    if result.returncode != 0:
        failures.append(suite)
    print()

print("=" * 46)
if failures:
    print("失败的套件:", ", ".join(failures))
    sys.exit(1)
print(f"全部通过：{', '.join(SUITES)}")
sys.exit(0)
