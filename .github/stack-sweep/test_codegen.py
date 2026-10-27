import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disasm import load, parse, renames_from
from fndiff import classification, report


def symbols(*entries):
    return "\n".join(f"{address:016x} l     F .text\t{size:016x}              {name}" for address, size, name in entries)


def disassembly(functions):
    lines = []
    for address, name, body in functions:
        lines.append(f"{address:016x} <{name}>:")
        for offset, text in enumerate(body):
            lines.append(f"   {address + 4 * offset:x}:\t{text}")
        lines.append("")
    return "\n".join(lines)


def binary(body, *entries, relocations="", name="f", renames=()):
    return parse(disassembly([(0x1000, name, body)]), symbols((0x1000, 4 * len(body), name), *entries),
                 relocations, list(renames))


def compare(base, candidate, name="f"):
    unresolved = (base.unresolved[name], candidate.unresolved[name])
    return classification(base.functions[name], candidate.functions[name], unresolved)


class Detected(unittest.TestCase):
    def test_a_changed_callee(self):
        base = binary(["bl\t2000 <a>"], (0x2000, 8, "a"))
        candidate = binary(["bl\t2000 <b>"], (0x2000, 8, "b"))
        self.assertEqual(compare(base, candidate), "differing")

    def test_a_changed_constant(self):
        base = binary(["mov\tw8, #0xc                   \t// #12"])
        candidate = binary(["mov\tw8, #0xd                   \t// #13"])
        self.assertEqual(compare(base, candidate), "differing")

    def test_a_call_into_a_veneer(self):
        base = binary(["bl\t2000 <a>"], (0x2000, 8, "a"))
        candidate = binary(["bl\t3000 <e843419@0001_0000_8>"], (0x2000, 8, "a"), (0x3000, 8, "e843419@0001_0000_8"))
        self.assertEqual(compare(base, candidate), "differing")

    def test_an_adrp_pair_naming_another_global_on_the_same_page(self):
        globals_ = [(0x10010, 8, "first"), (0x10018, 8, "second")]
        base = binary(["adrp\tx0, 10000 <first-0x10>", "ldr\tx0, [x0, #16]"], *globals_)
        candidate = binary(["adrp\tx0, 10000 <first-0x10>", "ldr\tx0, [x0, #24]"], *globals_)
        self.assertEqual(compare(base, candidate), "differing")

    def test_a_rip_relative_operand_naming_another_global(self):
        globals_ = [(0x20010, 8, "first"), (0x20018, 8, "second")]
        base = binary(["mov    0x1f00c(%rip),%rax        # 20010 <first>"], *globals_)
        candidate = binary(["mov    0x1f014(%rip),%rax        # 20018 <second>"], *globals_)
        self.assertEqual(compare(base, candidate), "differing")

    def test_a_function_missing_from_one_side(self):
        base = binary(["ret"])
        candidate = binary(["ret"], name="g")
        output = io.StringIO()
        self.assertTrue(report(base, candidate, ["^f$"], output))
        self.assertIn("MATCHES NOTHING IN CANDIDATE", output.getvalue())

    def test_a_name_collision(self):
        functions = [(0x1000, "f.llvm.1", ["ret"]), (0x1004, "f.llvm.2", ["nop"])]
        base = parse(disassembly(functions), symbols((0x1000, 4, "f.llvm.1"), (0x1004, 4, "f.llvm.2")), "", [])
        candidate = binary(["ret"])
        self.assertEqual(base.collisions(["f"]), ["f"])
        output = io.StringIO()
        self.assertTrue(report(base, candidate, ["^f$"], output))
        self.assertIn("COLLISION", output.getvalue())

    def test_a_failing_objdump(self):
        environment = dict(os.environ)
        try:
            os.environ["OBJDUMP"] = "false"
            with self.assertRaises(SystemExit):
                load("binary", [])
            os.environ["OBJDUMP"] = "/nonexistent/objdump"
            with self.assertRaises(SystemExit):
                load("binary", [])
        finally:
            os.environ.clear()
            os.environ.update(environment)


class Unresolved(unittest.TestCase):
    def test_a_reference_into_no_symbol(self):
        base = binary(["adrp\tx0, 50000 <f+0x4f000>", "add\tx0, x0, #0x348"])
        self.assertEqual(base.unresolved["f"], 1)
        self.assertEqual(base.functions["f"][1], "add x0, x0, ?0x50348")

    def test_an_adrp_whose_page_is_never_completed(self):
        body = ["adrp\tx0, 10000 <g>", "mov\tx1, x0", "ldr\tx2, [x1, #16]"]
        base = binary(body, (0x10010, 8, "g"))
        self.assertEqual(base.unresolved["f"], 1)
        self.assertEqual(base.functions["f"][0], "adrp x0, ?0x10000")

    def test_a_join_point_ends_the_pairing(self):
        body = ["adrp\tx0, 10000 <g>", "b\t100c <f+0xc>", "nop", "add\tx0, x0, #0x10"]
        base = binary(body, (0x10010, 8, "g"))
        self.assertEqual(base.functions["f"][0], "adrp x0, ?0x10000")
        self.assertEqual(base.functions["f"][3], "add x0, x0, #0x10")

    def test_a_call_ends_the_pairing(self):
        body = ["adrp\tx0, 10000 <g>", "bl\t2000 <a>", "add\tx0, x0, #0x10"]
        base = binary(body, (0x10010, 8, "g"), (0x2000, 8, "a"))
        self.assertEqual(base.functions["f"][0], "adrp x0, ?0x10000")

    def test_an_anonymous_constant(self):
        base = binary(["adrp\tx0, 10000 <g>", "add\tx0, x0, #0x10"], (0x10010, 8, "anon." + "0" * 32 + ".5"))
        self.assertEqual(base.unresolved["f"], 1)

    def test_differences_only_in_unresolved_operands_are_classified_apart(self):
        base = binary(["adrp\tx0, 50000 <f+0x4f000>", "add\tx0, x0, #0x348"])
        candidate = binary(["adrp\tx0, 60000 <f+0x5f000>", "add\tx0, x0, #0x308"])
        self.assertEqual(compare(base, candidate), "unresolved only")

    def test_unresolved_pages_differing_at_one_low_offset(self):
        base = binary(["adrp\tx0, 50000 <f+0x4f000>", "add\tx0, x0, #0x348"])
        candidate = binary(["adrp\tx0, 60000 <f+0x5f000>", "add\tx0, x0, #0x348"])
        self.assertNotEqual(base.functions["f"], candidate.functions["f"])
        self.assertEqual(compare(base, candidate), "unresolved only")

    def test_an_unresolved_reference_at_one_address_on_both_sides(self):
        base = binary(["adrp\tx0, 50000 <f+0x4f000>", "add\tx0, x0, #0x348"])
        candidate = binary(["adrp\tx0, 50000 <f+0x4f000>", "add\tx0, x0, #0x348"])
        self.assertEqual(compare(base, candidate), "unresolved only")

    def test_an_unresolved_rip_relative_operand_keeps_its_address(self):
        base = binary(["lea    0x4f000(%rip),%rax        # 50348 <f+0x4f348>"])
        self.assertEqual(base.unresolved["f"], 1)
        self.assertEqual(base.functions["f"], ["lea ?0x50348(%rip),%rax"])


class Equal(unittest.TestCase):
    def test_a_rip_relative_displacement_moved_by_layout(self):
        base = binary(["lea    0x1f00c(%rip),%rax        # 20010 <g>"], (0x20010, 8, "g"))
        candidate = binary(["lea    0x2f00c(%rip),%rax        # 30010 <g>"], (0x30010, 8, "g"))
        self.assertEqual(compare(base, candidate), "identical")
        self.assertEqual(base.functions["f"], ["lea @g(%rip),%rax"])

    def test_an_adrp_pair_moved_by_layout(self):
        base = binary(["adrp\tx0, 10000 <g-0x10>", "add\tx0, x0, #0x10", "ldr\tx1, [x0, #8]"], (0x10010, 16, "g"))
        candidate = binary(["adrp\tx0, 20000 <g-0x20>", "add\tx0, x0, #0x20", "ldr\tx1, [x0, #8]"],
                           (0x20020, 16, "g"))
        self.assertEqual(compare(base, candidate), "identical")
        self.assertEqual(base.functions["f"], ["adrp x0, @page", "add x0, x0, @g", "ldr x1, [x0, #8]"])

    def test_a_branch_within_the_function_moved_by_layout(self):
        base = parse(disassembly([(0x1000, "f", ["b.ne\t1008 <f+0x8>   \t// b.any", "nop", "ret"])]),
                     symbols((0x1000, 12, "f")), "", [])
        candidate = parse(disassembly([(0x5000, "f", ["b.ne\t5008 <f+0x8>   \t// b.any", "nop", "ret"])]),
                          symbols((0x5000, 12, "f")), "", [])
        self.assertEqual(compare(base, candidate), "identical")

    def test_a_got_slot_resolved_through_its_relocation(self):
        relocations = "0000000000040000 R_AARCH64_GLOB_DAT  malloc@GLIBC_2.17"
        base = binary(["adrp\tx0, 40000 <_GLOBAL_OFFSET_TABLE_>", "ldr\tx0, [x0]"], relocations=relocations)
        self.assertEqual(base.functions["f"][1], "ldr x0, [x0, @got:malloc@GLIBC_2.17]")

    def test_a_plt_stub(self):
        base = binary(["bl\t19300 <memset@plt>"])
        self.assertEqual(base.functions["f"], ["bl @memset@plt"])

    def test_a_call_into_folded_code_named_differently(self):
        base = binary(["bl\t2000 <z>"], (0x2000, 8, "a"), (0x2000, 8, "z"))
        candidate = binary(["bl\t3000 <b>"], (0x3000, 8, "a"), (0x3000, 8, "b"))
        self.assertEqual(compare(base, candidate), "identical")
        self.assertEqual(base.functions["f"], ["bl @a"])

    def test_a_renamed_candidate(self):
        renames = renames_from(["spawn_untimed=spawn"])
        base = binary(["bl\t2000 <spawn>"], (0x2000, 8, "spawn"), name="spawn::{closure#0}")
        candidate = binary(["bl\t2000 <spawn_untimed>"], (0x2000, 8, "spawn_untimed"),
                           name="spawn_untimed::{closure#0}", renames=renames)
        self.assertEqual(compare(base, candidate, "spawn::{closure#0}"), "identical")


unittest.main()
