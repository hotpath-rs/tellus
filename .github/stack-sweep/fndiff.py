"""Instruction diff and mnemonic deltas of the functions matching each regex, compared by disasm.py:
fndiff.py BASE CANDIDATE REGEX... [--rename REGEX=REPLACEMENT]... A function is identical (with no
unresolved reference), differs only in unresolved operands (marked `?`, which may or may not be
layout), or differs. Exits non-zero when a regex matches no function in the base or in the
candidate, or on a name collision among the matches.
"""

import argparse
import difflib
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disasm import load, renames_from

UNRESOLVED = re.compile(r"\?-?[0-9a-fx]+")
CALL = re.compile(r"^(bl|blr|call|callq) ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("candidate")
    parser.add_argument("patterns", nargs="+")
    parser.add_argument("--rename", action="append", help="REGEX=REPLACEMENT, applied to the candidate")
    args = parser.parse_args()
    base, candidate = load(args.base, []), load(args.candidate, renames_from(args.rename))
    sys.exit(1 if report(base, candidate, args.patterns, sys.stdout) else 0)


def report(base, candidate, patterns, out):
    """Writes the comparison to out; returns whether it failed."""
    a, b = base.functions, candidate.functions
    failed = False
    totals = Counter()
    for pattern in patterns:
        regex = re.compile(pattern)
        names_a = {name for name in a if regex.search(name)}
        names_b = {name for name in b if regex.search(name)}
        print(f"== {pattern}: {len(names_a)} in base, {len(names_b)} in candidate", file=out)
        for side, names in (("base", names_a), ("candidate", names_b)):
            if not names:
                print(f"   MATCHES NOTHING IN {side.upper()}", file=out)
                failed = True
        collisions = set(base.collisions(names_a)) | set(candidate.collisions(names_b))
        for name in sorted(names_a | names_b):
            if name in collisions:
                print(f"COLLISION: {name[:120]}", file=out)
                totals["colliding"] += 1
                failed = True
            elif name not in b or name not in a:
                print(f"ONLY IN {'BASE' if name in a else 'CANDIDATE'}: {name[:120]}", file=out)
                totals["only in one"] += 1
            else:
                unresolved = (base.unresolved[name], candidate.unresolved[name])
                kind = classification(a[name], b[name], unresolved)
                totals[kind] += 1
                totals["with unresolved references"] += any(unresolved)
                diff(name, kind, a[name], b[name], unresolved, out)
    print("== totals:", ", ".join(f"{key} {value}" for key, value in sorted(totals.items())) or "none", file=out)
    return failed


def classification(body_a, body_b, unresolved):
    """Never "identical" with an unresolved reference on either side, even at equal addresses."""
    if body_a == body_b and not any(unresolved):
        return "identical"
    masked_a = [UNRESOLVED.sub("?", line) for line in body_a]
    masked_b = [UNRESOLVED.sub("?", line) for line in body_b]
    return "unresolved only" if masked_a == masked_b else "differing"


def diff(name, kind, body_a, body_b, unresolved, out):
    """Prints the lines which differ beyond unresolved operands, those which differ only there
    when nothing else does."""
    matcher = difflib.SequenceMatcher(None, body_a, body_b, autojunk=False)
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")
    print(f"{kind}, {len(body_a)} -> {len(body_b)} insns, {changed} differing lines, "
          f"{unresolved[0]} -> {unresolved[1]} unresolved: {name[:110]}", file=out)
    if not changed:
        return
    if kind == "differing":
        masked_a = [UNRESOLVED.sub("?", line) for line in body_a]
        masked_b = [UNRESOLVED.sub("?", line) for line in body_b]
        matcher = difflib.SequenceMatcher(None, masked_a, masked_b, autojunk=False)
    opcodes = [opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"]
    changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in opcodes)
    counts_a = Counter(line.split()[0] for line in body_a)
    counts_b = Counter(line.split()[0] for line in body_b)
    deltas = {k: counts_b[k] - counts_a[k] for k in counts_a | counts_b if counts_b[k] != counts_a[k]}
    print("   mnemonic deltas:", dict(sorted(deltas.items())) or "none", file=out)
    calls_a = Counter(line.split(" ", 1)[1] for line in body_a if CALL.match(line))
    calls_b = Counter(line.split(" ", 1)[1] for line in body_b if CALL.match(line))
    for callee in sorted(calls_a | calls_b):
        if calls_a[callee] != calls_b[callee]:
            print(f"   calls {calls_a[callee]} -> {calls_b[callee]}: {callee[:150]}", file=out)
    if changed <= 60:
        for _, i1, i2, j1, j2 in opcodes:
            print(f"   @ base {i1}-{i2} / cand {j1}-{j2}", file=out)
            for line in body_a[i1:i2]:
                print("   - " + line, file=out)
            for line in body_b[j1:j2]:
                print("   + " + line, file=out)


if __name__ == "__main__":
    main()
