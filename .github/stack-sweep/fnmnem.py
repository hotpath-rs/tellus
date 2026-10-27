"""Mnemonic histogram difference of named functions: fnmnem.py A B regex..."""
import os, re, subprocess, sys
from collections import Counter
OBJDUMP = os.environ.get("OBJDUMP", "objdump")
def funcs(path):
    out = subprocess.run([OBJDUMP, "-d", "-C", "--no-show-raw-insn", path], capture_output=True, text=True).stdout
    fns, name = {}, None
    for line in out.splitlines():
        m = re.match(r"^[0-9a-f]+ <(.*)>:$", line)
        if m:
            name = re.sub(r"\.llvm\.\d+", "", m.group(1)); fns[name] = Counter(); continue
        m = re.match(r"^\s+[0-9a-f]+:\s+(\S+)", line)
        if name and m:
            fns[name][m.group(1)] += 1
    return fns
a, b = funcs(sys.argv[1]), funcs(sys.argv[2])
for pat in sys.argv[3:]:
    for name in sorted(n for n in a if re.search(pat, n) and n in b):
        d = {k: b[name][k] - a[name][k] for k in set(a[name]) | set(b[name]) if b[name][k] != a[name][k]}
        calls_a = [k for k in a[name] if k.startswith(("bl", "call"))]
        print(f"{sum(a[name].values())} -> {sum(b[name].values())}: {name[:90]}")
        print("   mnemonic deltas:", dict(sorted(d.items())) or "none")
