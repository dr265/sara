import sys
sys.path.insert(0, ".")
from actions.desktop import _execute_generated_code

# 1. model-style code WITH import statements (the bug from the screenshot)
r1 = _execute_generated_code(
    "import pyautogui\nimport time\ntime.sleep(0.1)\nprint('sandbox-ok')")
assert "sandbox-ok" in r1, r1

# 2. from os.path import join
r2 = _execute_generated_code(
    "from os.path import join\nprint(join('a', 'b'))")
assert "a" in r2 and "b" in r2, r2

# 3. shutil proxy: copy allowed, rmtree absent
r3 = _execute_generated_code(
    "import shutil\nprint(hasattr(shutil, 'copy2'), hasattr(shutil, 'rmtree'))")
assert "True False" in r3, r3

# 4. os / subprocess refused
r4 = _execute_generated_code("import os")
assert "not allowed" in r4, r4
r5 = _execute_generated_code("import subprocess")
assert "not allowed" in r5, r5

# 5. bare `import os.path` refused (it would bind the full os module)
r6 = _execute_generated_code("import os.path")
assert "not allowed" in r6 or "os_path" in r6, r6

# 6. pre-injected names still work without any import (the intended style)
r7 = _execute_generated_code("print('ok', Path('.') != None, hasattr(pyautogui, 'click'))")
assert "ok True True" in r7, r7

print("SANDBOX_TESTS_ALL_PASS")
