"""Per-function instruction counts of two binaries: fncounts.py A B (env OBJDUMP for a cross objdump)."""
import re, subprocess, sys
def counts(path):
    out = subprocess.run([__import__("os").environ.get("OBJDUMP","objdump"), "-d", "-C", "--no-show-raw-insn", path], capture_output=True, text=True).stdout
    fns, name = {}, None
    for line in out.splitlines():
        m = re.match(r"^[0-9a-f]+ <(.*)>:$", line)
        if m:
            name = re.sub(r"\.llvm\.\d+", "", m.group(1)); fns[name] = 0; continue
        if name and re.match(r"^\s+[0-9a-f]+:\s", line):
            fns[name] += 1
    return fns
a, b = counts(sys.argv[1]), counts(sys.argv[2])
sel = lambda n: ("tellus" in n or "messaging" in n or "flume" in n) and "drop_glue" not in n
only_a = sorted(n for n in a if n not in b and sel(n)); only_b = sorted(n for n in b if n not in a and sel(n))
diff = sorted((n, a[n], b[n]) for n in a if n in b and a[n] != b[n] and sel(n))
same = sum(1 for n in a if n in b and a[n] == b[n] and sel(n))
print(f"functions only in A: {len(only_a)}; only in B: {len(only_b)}; differing: {len(diff)}; identical: {same}")
for n in only_a: print("A only", a[n], n[:150])
for n in only_b: print("B only", b[n], n[:150])
for n, x, y in diff: print(f"differ {x} -> {y}", n[:150])
