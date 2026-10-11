"""按顺序在各自独立的进程里运行所有测试套件。

为什么每个套件要单独起进程：AppTest 的脚本运行器会复用 sys.modules 里已缓存的
AI_partner —— 同一个进程里只有第一次运行会真正执行应用脚本，之后的套件会拿到
上一次的模块状态，断言就失去意义（表现为"单独跑通过、连着跑失败"）。

为什么还要给每个套件一个**独立的临时根目录**（AI_PARTNER_TEST_TMP）：
_harness 在 import 时就会 reset_tmp()，而 reset_tmp() 会**清空整个临时根目录**。
如果所有套件共用 tests/.tmp，那么任何"再起子进程"的套件（例如
test_package_import.py 会用 subprocess 验证导入）都会把**父套件正在用的存档目录
一起清掉**，导致后续断言看到空会话列表 —— 症状是"失败套件每次都不一样、还间歇
性通过"，极难排查（真踩过）。

运行：
    .venv\\Scripts\\python.exe tests\\run_tests.py
"""

import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SUITES = ("test_logic.py", "test_smoke.py", "test_row_render.py", "test_branch_ui.py",
          "test_avatar_ui.py", "test_rerun_render.py", "test_auth_ui.py",
          "test_user_isolation.py", "test_mongo_ui.py",
          "test_secrets_format.py", "test_session_switch.py",
          "test_storage_budget.py", "test_no_data_loss.py",
          "test_package_import.py", "test_delete_flow.py",
          "test_delete_mongo.py", "test_partition_migrate.py", "test_switch_flow.py", "test_draft_gc.py", "test_draft_safety.py", "test_sidebar_layout.py")

failures = []
for index, suite in enumerate(SUITES):
    print(f"===== {suite} =====")
    # 每个套件一个独立根目录：这样套件内再起子进程也不会清掉本套件的存档
    env = {**os.environ, "AI_PARTNER_TEST_TMP": str(TESTS_DIR / f".tmp_run{index}")}
    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / suite)],
        cwd=str(TESTS_DIR.parent),
        env=env,
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
