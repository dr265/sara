import io, re, os
# Replace hardcoded secret-looking literals in scratch scripts with env lookups.
targets = []
for root, dirs, files in os.walk("scratch"):
    for f in files:
        if f.endswith(".py"):
            targets.append(os.path.join(root, f))
pat = re.compile(r'("|\')((?:sk-|mnfst_|7e7a4|cc_)[A-Za-z0-9_\-]{12,})\1')
for path in targets:
    try:
        s = io.open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    new = pat.sub(lambda m: f'{m.group(1)}os.environ.get("SCRATCH_API_KEY", ""){m.group(1)}', s)
    if new != s:
        if "import os" not in new:
            new = "import os\n" + new
        io.open(path, "w", encoding="utf-8", newline="\n").write(new)
        print("scrubbed:", path)
