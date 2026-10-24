"""Structural diff of modified files vs git HEAD (safety check)."""
import re
import subprocess

REPO = r"C:\Users\HP\sara-ai-platform"


def git_show(path: str) -> str:
    out = subprocess.run(["git", "-C", REPO, "show", f"HEAD:{path}"],
                         capture_output=True)
    return out.stdout.decode("utf-8")


def check(rel: str) -> None:
    orig = git_show(rel)
    cur = open(fr"{REPO}\{rel}", encoding="utf-8").read()
    orig_defs = set(re.findall(r"def (\w+)\(", orig))
    cur_defs = set(re.findall(r"def (\w+)\(", cur))
    print(f"[{rel}]")
    print("  missing methods:", sorted(orig_defs - cur_defs))
    print("  added methods:", sorted(cur_defs - orig_defs))
    orig_tools = set(re.findall(r'"name":\s*"(\w+)"', orig))
    cur_tools = set(re.findall(r'"name":\s*"(\w+)"', cur))
    print("  missing tool names:", orig_tools - cur_tools)
    orig_imp = set(re.findall(r"from ([\w.]+) import", orig))
    cur_imp = set(re.findall(r"from ([\w.]+) import", cur))
    print("  missing imports:", orig_imp - cur_imp)
    print(f"  lines: {len(orig.splitlines())} -> {len(cur.splitlines())}")


check("sara_desktop/main.py")
check("sara_desktop/ui.py")
