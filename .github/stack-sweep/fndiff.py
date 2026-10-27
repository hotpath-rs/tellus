"""Stripped disassembly diff of named functions: fndiff.py A B regex..."""
import os, re, subprocess, sys, difflib
OBJDUMP = os.environ.get("OBJDUMP", "objdump")
def funcs(path):
    out = subprocess.run([OBJDUMP, "-d", "-C", "--no-show-raw-insn", path], capture_output=True, text=True).stdout
    fns, name = {}, None
    for line in out.splitlines():
        m = re.match(r"^[0-9a-f]+ <(.*)>:$", line)
        if m:
            name = re.sub(r"\.llvm\.\d+", "", m.group(1)); fns[name] = []; continue
        m = re.match(r"^\s+[0-9a-f]+:\s+(.*)$", line)
        if name and m:
            ins = m.group(1)
            ins = re.sub(r"<[^>]*>", "<SYM>", ins)
            ins = re.sub(r"0x[0-9a-f]{4,}", "ADDR", ins)
            ins = re.sub(r"\b[0-9a-f]{5,}\b", "ADDR", ins)
            fns[name].append(ins)
    return fns
a, b = funcs(sys.argv[1]), funcs(sys.argv[2])
for pat in sys.argv[3:]:
    for name in sorted(n for n in a if re.search(pat, n)):
        if name not in b:
            print(f"ONLY IN A: {name[:120]}"); continue
        sm = difflib.SequenceMatcher(None, a[name], b[name], autojunk=False)
        changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
        print(f"{len(a[name])} -> {len(b[name])} insns, {changed} differing lines: {name[:110]}")
        if changed and changed <= 40:
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    for l in a[name][i1:i2]: print("   - " + l)
                    for l in b[name][j1:j2]: print("   + " + l)
