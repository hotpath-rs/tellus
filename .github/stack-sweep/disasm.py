"""The functions of a binary as comparable instruction lists, from objdump (env OBJDUMP for a cross
objdump). Function names are normalized. Every address operand is replaced by the symbol it points
into plus the offset, or replaced by its full address marked `?` and counted as unresolved, so equal
lists without unresolved references mean equal code and equal references, whatever the layout.
"""

import bisect
import os
import re
import subprocess
import sys

HEADER = re.compile(r"^([0-9a-f]+) <(.*)>:$")
INSTRUCTION = re.compile(r"^\s+([0-9a-f]+):\s+(.*?)\s*$")
SYMBOL = re.compile(r"^([0-9a-f]+) .{7} (\S+)\t([0-9a-f]+) +(?:\.hidden )?(.*)$")
RELOCATION = re.compile(r"^([0-9a-f]+) +(R_\S+) +(.*)$")
COMMENT = re.compile(r"\s*//.*$")
ANNOTATED = re.compile(r"\b([0-9a-f]+) <(.*)>$")
RIP_COMMENT = re.compile(r"\s+# ([0-9a-f]+) <(.*)>$")
RIP = re.compile(r"-?0x[0-9a-f]+\(%rip\)")
ADRP = re.compile(r"^adrp\s+x(\d+), ([0-9a-f]+) <.*>$")
LO12_ADD = re.compile(r"^add\s+[xw](\d+), x(\d+), #(0x[0-9a-f]+|\d+)$")
LO12_MEMORY = re.compile(r"^(\w+)\s+(.*)\[x(\d+)(?:, #(0x[0-9a-f]+|\d+))?\]$")
ANONYMOUS = re.compile(r"anon\.[0-9a-f]{32}\.")
REGISTER = re.compile(r"\b[xw](\d+)\b")
CALLS = {"bl", "blr", "call", "callq"}


class Binary:
    """functions: normalized name to instructions; raw_names: normalized name to the names it
    stands for, more than one being a collision; unresolved: normalized name to the number of
    references kept raw."""

    def __init__(self):
        self.functions = {}
        self.raw_names = {}
        self.unresolved = {}

    def collisions(self, names):
        return sorted(name for name in names if len(self.raw_names.get(name, ())) > 1)


def renames_from(pairs):
    renames = []
    for pair in pairs or []:
        pattern, separator, replacement = pair.rpartition("=")
        if not separator:
            sys.exit(f"--rename expects REGEX=REPLACEMENT, split at the last =, got {pair}")
        try:
            renames.append((re.compile(pattern), replacement))
        except re.error as error:
            sys.exit(f"--rename {pair}: {error}")
    return renames


def normalize(name, renames):
    name = re.sub(r"\.llvm\.\d+", "", name)
    name = re.sub(r"::h[0-9a-f]{16}$", "", name)
    for pattern, replacement in renames:
        name = pattern.sub(replacement, name)
    return name


def objdump(path, *args):
    tool = os.environ.get("OBJDUMP", "objdump")
    try:
        process = subprocess.run([tool, *args, path], capture_output=True, text=True)
    except OSError as error:
        sys.exit(f"{tool}: {error}")
    if process.returncode != 0:
        if "-R" in args and "not a dynamic object" in process.stderr:
            return ""
        sys.exit(f"{tool} {' '.join(args)} {path} failed:\n{process.stderr}")
    return process.stdout


def load(path, renames):
    binary = parse(
        objdump(path, "-d", "-C", "--no-show-raw-insn"),
        objdump(path, "-t", "-C"),
        objdump(path, "-R", "-C"),
        renames,
    )
    if not binary.functions:
        sys.exit(f"no functions disassembled from {path}")
    return binary


def parse(disassembly, symbol_table, relocations, renames):
    resolve = Resolver(symbol_table, relocations, renames)
    binary = Binary()
    for raw_name, body in blocks(disassembly):
        name = normalize(raw_name, renames)
        binary.raw_names.setdefault(name, []).append(raw_name)
        binary.functions[name], binary.unresolved[name] = instructions(body, resolve)
    return binary


def blocks(disassembly):
    name, body = None, []
    for line in disassembly.splitlines():
        header = HEADER.match(line)
        if header:
            if name is not None:
                yield name, body
            name, body = header.group(2), []
        elif name is not None:
            instruction = INSTRUCTION.match(line)
            if instruction:
                body.append((int(instruction.group(1), 16), instruction.group(2)))
    if name is not None:
        yield name, body


def instructions(body, resolve):
    if not body:
        return [], 0
    first, last = body[0][0], body[-1][0]
    targets = set()
    for _, text in body:
        annotated = ANNOTATED.search(text)
        if annotated and first <= int(annotated.group(1), 16) <= last:
            targets.add(int(annotated.group(1), 16))

    out, unresolved = [], 0
    pages = {}

    def release(register):
        nonlocal unresolved
        page, index, used = pages.pop(register)
        if not used:
            out[index] = f"adrp x{register}, ?{page:#x}"
            unresolved += 1

    for address, text in body:
        if address in targets:
            for register in list(pages):
                release(register)
        text = " ".join(COMMENT.sub("", text).split())
        mnemonic = text.split(" ", 1)[0]

        adrp = ADRP.match(text)
        if adrp:
            register = int(adrp.group(1))
            if register in pages:
                release(register)
            pages[register] = (int(adrp.group(2), 16), len(out), False)
            out.append(f"adrp x{register}, @page")
            continue

        paired = pair(text, pages, resolve)
        if paired:
            text, register, written, target = paired
            page, index, _ = pages[register]
            pages[register] = (page, index, True)
            if target is None:
                unresolved += 1
            for register in written:
                if register in pages:
                    release(register)
            out.append(text)
            continue

        for register in {int(r) for r in REGISTER.findall(text)} & set(pages):
            release(register)
        text, missed = references(text, resolve)
        unresolved += missed
        out.append(text)
        if mnemonic in CALLS:
            for register in list(pages):
                release(register)

    for register in list(pages):
        release(register)
    return out, unresolved


def pair(text, pages, resolve):
    """An add or memory access completing an adrp: (text, base register, registers written,
    resolved target or None), or None if the instruction completes no adrp."""
    add = LO12_ADD.match(text)
    if add and int(add.group(2)) in pages:
        base = int(add.group(2))
        address = pages[base][0] + int(add.group(3), 0)
        target = resolve(address)
        operand = f"@{target}" if target else f"?{address:#x}"
        return f"add x{add.group(1)}, x{base}, {operand}", base, [int(add.group(1))], target
    memory = LO12_MEMORY.match(text)
    if memory and int(memory.group(3)) in pages:
        mnemonic, operands, base = memory.group(1), memory.group(2), int(memory.group(3))
        address = pages[base][0] + int(memory.group(4) or "0", 0)
        target = resolve(address)
        operand = f"@{target}" if target else f"?{address:#x}"
        written = [] if mnemonic.startswith("st") else [int(r) for r in REGISTER.findall(operands)]
        return f"{mnemonic} {operands}[x{base}, {operand}]", base, written, target
    return None


def references(text, resolve):
    """The instruction with its address operands resolved, and the number left raw."""
    comment = RIP_COMMENT.search(text)
    if comment and RIP.search(text):
        target = resolve(int(comment.group(1), 16), comment.group(2))
        operand = f"@{target}" if target else f"?0x{comment.group(1)}"
        return RIP.sub(f"{operand}(%rip)", text[: comment.start()]), int(target is None)
    annotated = ANNOTATED.search(text)
    if annotated:
        target = resolve(int(annotated.group(1), 16), annotated.group(2))
        operand = f"@{target}" if target else f"?0x{annotated.group(1)}"
        return text[: annotated.start()] + operand, int(target is None)
    return text, 0


class Resolver:
    """An address to the normalized name of the symbol containing it plus the offset, a GOT slot
    outside every symbol to the target its relocation names, a PLT stub to its name; None for
    anything else, including an `anon.<hash>` constant, whose hash differs between builds."""

    def __init__(self, symbol_table, relocations, renames):
        self.renames = renames
        symbols = []
        for line in symbol_table.splitlines():
            symbol = SYMBOL.match(line)
            if symbol and symbol.group(2) not in ("*UND*", "*ABS*"):
                symbols.append((int(symbol.group(1), 16), int(symbol.group(3), 16), symbol.group(4)))
        symbols.sort()
        self.starts = [start for start, _, _ in symbols]
        self.symbols = symbols
        self.slots = {}
        for line in relocations.splitlines():
            relocation = RELOCATION.match(line)
            if relocation:
                self.slots[int(relocation.group(1), 16)] = relocation.group(3)

    def __call__(self, address, annotation=""):
        target = self.symbol(address)
        if target is not None:
            return target or None
        slot = self.slots.get(address)
        if slot is not None:
            absolute = re.fullmatch(r"\*ABS\*\+0x([0-9a-f]+)", slot)
            target = self.symbol(int(absolute.group(1), 16)) if absolute else normalize(slot, self.renames)
            return f"got:{target}" if target else None
        if re.fullmatch(r"[^+]+@plt", annotation):
            return normalize(annotation, self.renames)
        return None

    def symbol(self, address):
        """The symbol plus offset, "" for a symbol whose name is not stable across builds, None
        outside every symbol. Of several names at one address (identical code folding), the
        smallest, since the set of aliases differs between builds."""
        index = bisect.bisect_right(self.starts, address)
        for start in sorted(set(self.starts[max(0, index - 64) : index]), reverse=True)[:8]:
            first = bisect.bisect_left(self.starts, start)
            aliases = [
                name
                for alias_start, size, name in self.symbols[first:index]
                if alias_start == start and (address < start + size or start == address)
            ]
            if not aliases:
                continue
            stable = sorted(normalize(name, self.renames) for name in aliases if not ANONYMOUS.match(name))
            if not stable:
                return ""
            offset = address - start
            return f"{stable[0]}+{offset:#x}" if offset else stable[0]
        return None
