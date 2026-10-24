# Actor ID alignment: investigation report

This report describes the investigation from the first suspicious benchmark to the decision to
keep `main` as it is. The [summary](README.md) has the result. Sections 1 to 6 come from
measurements on one aarch64 machine, sections 7 to 9 add GitHub's runners. The raw data is not
published.

## 1. Starting point

PR #174 adds timers to actors. CI's Criterion comparison showed the messaging benchmarks 1 to 14%
slower, and hotpath reported 27% more allocated bytes in the profiling workload. CI runs on a
shared runner, where identical code has moved rows by up to 36%, so we measured locally:
main's `messaging` bench against main (ceb69f0) and against the PR (84eb511), three alternating
runs each.

| Row | Change, best of 3 |
|---|---|
| `queued/never/4`, `queued/every/4`, `queued/never/16`, `queued/every/16` | +54 to +57% |
| `flood/unbounded` | -24% |
| `flood/bounded` | -18% |

A per-message cost of the timer code could not make one row faster and another slower through the
same run loop. Something else was going on.

## 2. Wrong turns

We bisected the PR on `queued/never/4`, alternating three runs per build.

| Build | Change against main |
|---|---|
| PR | +55% |
| PR without the timer branch in the select | +55% |
| PR without the gate | +48% |
| PR plus one unused 16-byte field in `ActorContext` | +7 to +8% |
| main plus the same field | within ±1% |

The timer code was not the cause, but an unused field moved the result by 48 percentage points.
From this we first concluded that timers cost about 7%. That was wrong: the field only changed the
layout, and the 7% came from one other layout.

A sweep of the padding (0 to 64 bytes in steps of 8) confirmed the sensitivity. Main stayed at 5.6
to 6.1 ms on `queued/never/4` for every size. The PR was slow (9.0 ms) only without padding and at
6.1 to 6.4 ms with any.

Two size explanations failed. Tokio aligns a task's cell to 128 bytes on aarch64, so a growing
future changes the cell's size in steps. But the PR with and without padding had the same 1280-byte
cell, and only one of them was slow. The future's size did not explain it either: main padded by 16
bytes had the PR's Probing future size of 1088 bytes and ran at main's speed.

Next we rebuilt with all functions and branch targets aligned to 64 bytes
(`-C llvm-args=-align-all-functions=6 -align-all-nofallthru-blocks=6`). The PR then ran at main's
speed, and the padded PR became the slow one. We concluded that code placement was the cause. That
was also wrong: the flags change more than instruction addresses, and they also moved the stack
temporaries that turned out to matter.

## 3. Isolating the cause

**Shifting the stack.** A small library preloaded with `LD_PRELOAD` intercepts `pthread_create`
and reserves `STACK_SHIFT` extra bytes before each new thread starts (see the appendix). The
benchmark binary stays unchanged, while every worker stack moves by the given amount. On
`queued/never/4`, pinned to one CPU:

| Binary | Slow shifts | Fast shifts |
|---|---|---|
| PR | 48: 6.14 ms, 4144: 6.19 ms | 64: 4.18 ms, 4160: 4.22 ms |
| main | 96: 5.83 ms, 4192: 5.91 ms | 0: 3.49 ms, 224: 3.73 ms |

The penalty repeats every 4096 bytes, the page size. It persists on a single CPU, so contention
between cores is not the cause.

**The instruction.** In the PR binary, `Mailbox::recv` passes a stack buffer at `sp + 12` to
flume, and copies the ID out of it with `ldur q0, [sp, #13]`. At the slow shift, that
address lies at page offset `0xffd`, so the 16-byte load reads across the page boundary.

**The control.** We patched the PR binary to move only that temporary, keeping every instruction
at its address. The time dropped from 6.78 to 4.73 ms. A control patch restoring the temporary's
original address brought it back to 6.81 ms.

**Why the copy is unaligned.** `ActorId` wraps a `Uuid`, a `[u8; 16]` with alignment 1. So
`Incoming<()>` is 17 bytes with alignment 1, and the compiler moves the ID with unaligned 16-byte
loads and stores. Several such copies lie along the receive path, so a change in one stack frame
can move the problem from one copy to another.

## 4. The plan and its criteria

The candidate was `#[repr(align(16))]` on `ActorId` and nothing else. Before measuring, we fixed:

- **Batch:** a set of stack shifts measured in 3 repetitions, rotating the order of the builds. Its
  figure is the median of all its runs.
- **Throughput tolerance:** the candidate's batch figure is at most 3% above base's. A row between
  2% and 4% is borderline and gets two more batches; the median of the three batch figures decides.
- **Instability:** if one build's per-repetition figures in a batch spread by more than 3%, the
  comparison of that row is inconclusive.
- **Robustness:** sweep all 256 shifts within a page in steps of 16 bytes. Flag every shift more
  than 15% above the build's median across the sweep. Repeat every flagged shift and its neighbors
  five times for both builds. At every flagged shift, the median of the repeats must stay within
  15% of the build's median.
- **Memory:** the larger mailbox items are accepted if robustness improves and throughput meets the
  tolerance. All other layout changes are reported.

Runs without pinning use the bench's 4 worker threads. Criterion runs with a warm-up of 0.2 s, a
measurement of 0.5 s and 10 samples, and we record its median.

## 5. Results

**Step 1, reproduction.** The binaries from section 3, five pinned runs per shift, gave the
figures in the table there. Every slow shift was at least 30% slower than the fast shifts of the
same binary.

**Step 2, layouts.** Base is main at 39aec83; the candidate adds the attribute.

| Type | Base | Candidate |
|---|---|---|
| `ActorId` | 16 bytes, align 1 | 16, align 16 |
| `Incoming<()>` | 17, align 1 | 32, align 16 |
| `Incoming<u64>` | 24, align 8 | 32, align 16 |
| `Incoming<[u64; 6]>` | 56, align 8 | 64, align 16 |
| `Incoming<String>` | 24, align 8 | 32, align 16 |
| `ActorRef<_>` with `cluster` | 72, align 8 | 80, align 16 |
| `ActorContext<_>` with `cluster` | 168, align 8 | 176, align 16 |
| `flume::Hook<Incoming<_>, dyn Signal>` | 28, align 4 | 64, align 16 |
| `cluster::reply::PendingReply` | 192, align 8 | 224, align 16 |
| `cluster::reply::ReplyTag` | 32, align 8 | 48, align 16 |

These are the types emitted by `-Zprint-type-sizes` for the all-features library build and the
messaging bench build, plus explicit probes of `Incoming<M>`. 166 types changed in the library
build, all of them holding an `ActorId`.

**Step 3, generated copies.** We disassembled the receive path of the `Probing` actor in both
builds: the actor's poll, the select poll, `Mailbox::recv`, and flume's functions for the item
type. For accesses through SP, the AArch64 ABI keeps SP 16-byte aligned, so an offset divisible by
16 proves an aligned address. Other base registers were traced back to SP or to a type's alignment.

- **Base:** misaligned 16-byte copies of the item on the receive path, among them
  `ldur q0, [sp, #13]` and `stur q0, [x20, #1]` in `Mailbox::recv`, `ldur q0, [sp, #12]` in the
  select poll, and copies at offsets 1, 9 and 10 in flume's `Shared::recv`, `pull_pending` and
  `Hook::try_take`.
- **Candidate:** every 16-byte copy of an `Incoming` or `ActorId` on that path is proven aligned.
- **Misaligned accesses left in the candidate:**
  - flume's `AsyncSignal` copy when the receiver parks on an empty queue, also in base;
  - an item copy on flume's blocking send path, which only a full bounded channel takes (tellus
    channels are unbounded underneath);
  - `Receiver::drain` at termination;
  - an ID copy on the actor's failure path.

**Step 4, robustness.** The sweep covered `queued/never/4` and `flood/unbounded`.

| Build, row | Median | Flagged | Above +15% in the repeats |
|---|---|---|---|
| base, `queued/never/4` | 5.78 ms | 10 | 7: 96 +47%, 4064 +44%, 208 +41%, 2144 +31%, 944 +30%, 2176 +24%, 2192 +16% |
| candidate, `queued/never/4` | 5.74 ms | 9 | 2: 2080 +21%, 2064 +19% |
| base, `flood/unbounded` | 5.76 ms | 77 | 19, up to 3920 +52%, 3792 +42%, 4032 +37% |
| candidate, `flood/unbounded` | 5.60 ms | 10 | 2: 2112 +25%, 2160 +24% |

Base's stable penalties, with five nearly identical repeats each, do not occur in the candidate.
But the candidate exceeds 15% at four shifts. All four lie between shifts 2048 and 2192, where
both builds alternate between about 5.6 and about 7 ms from run to run. **The candidate fails the
robustness requirement.**

**Follow-up on the variation.** We tried to isolate it before going on.

- **Controls:** each affected run directly followed a run at shift 2208 with the same build. The
  candidate's controls stayed fast (`flood` 5.41 to 5.84 ms) while the affected shifts flipped. So
  the variation is tied to the shift, not to slow periods on the machine.
- **CPU placement:** it persists without restriction, on CPUs 0 to 4 and on CPU 0 alone (`flood`
  3.5 against 6.6 ms). Placement does change which shifts are affected.
- **Starting stack positions:** with ASLR off and a fixed environment, the main thread's stack sat
  at the same address in every run, and the worker stacks at offset 3824 within a page. `flood` at
  2112 was still slow in 10 of 20 base runs and 11 of 20 candidate runs.

This rules out the starting stack positions, not every stack-related mechanism: Criterion's own
stack variation per iteration is untested. Heap placement and scheduling also remain candidates.
We stopped here, without an explanation.

**Step 5, throughput and memory (characterization, not acceptance).** One batch per row over 16
shifts, 256 bytes apart; the payload rows are variants of `flood/unbounded` and `queued/never/4`.

| Row | Base | Candidate | Change |
|---|---|---|---|
| `flood/unbounded` | 5.811 ms | 5.577 ms | -4.0%, inconclusive (base spread 12.7%) |
| `flood/bounded` | 7.141 | 6.822 | -4.5% |
| `ping_pong/pairs/1` | 0.380 | 0.378 | -0.4% |
| `ping_pong/pairs/4` | 0.357 | 0.354 | -1.0%, inconclusive (candidate spread 3.4%) |
| `fan_out/workers/4` | 4.612 | 4.576 | -0.8% |
| `fan_out/workers/16` | 2.670 | 2.694 | +0.9% |
| `queued/never/4` | 5.860 | 5.833 | -0.5% |
| `queued/every/4` | 5.994 | 6.097 | +1.7% |
| `queued/never/16` | 5.868 | 5.865 | 0.0% |
| `queued/every/16` | 6.004 | 6.170 | +2.8% (three batches: +2.6%, +2.6%, +2.8%) |
| `payload_flood/u64` | 5.781 | 5.569 | -3.7% |
| `payload_flood/u64x6` | 7.471 | 7.401 | -0.9% |
| `payload_queued/u64/4` | 5.784 | 5.657 | -2.2% |
| `payload_queued/u64x6/4` | 7.281 | 6.921 | -4.9% |

Memory, as the process RSS delta of a backlog of 1,000,000 items in a fresh process (median of 5):

| Payload | Base | Candidate |
|---|---|---|
| `()` | 17.1 MB | 33.7 MB (+96%) |
| `u64` | 25.3 MB | 33.7 MB (+33%) |
| `[u64; 6]` | 58.9 MB | 67.1 MB (+14%) |

The allocation totals of the profiling workload, which include freed allocations, rose from 4.0 to
6.1 MB, almost all in the channel's buffers. `tell`, `reserve` and `receive_incoming` still
allocate 0 bytes.

## 6. The fallback: aligning `Incoming`

Aligning `Incoming` instead of `ActorId` would have spared `ActorRef`, `ActorContext` and the
cluster types: only 15 types changed, all of them `Incoming` or flume structures around it. The
mailbox items have the same sizes as with the aligned ID.

It does not work. The ID keeps alignment 1 and sits at offset 1 inside the enum, behind its tag,
so the compiler still copies it unaligned: `ldur q0, [sp, #1]` and `stur q0, [x20, #1]` in
`Mailbox::recv`, and copies at offsets 1, 2, 14 and 15 in flume. Measured on `queued/never/4`:

| Build | Median across the sweep | Of the fallback's 15 flagged shifts, above +15% in the repeats |
|---|---|---|
| base | 5.78 ms | 2 |
| aligned `ActorId` | 5.74 ms | 2 |
| aligned `Incoming` | 6.51 ms | 10, +22 to +45% |

On `queued/every/16`, the fallback was 25% slower than base.

## 7. The aligned ID on GitHub's runners

Based on sections 1 to 6 we first accepted the aligned ID, opened as #177, with x86_64 untested.
CI's Criterion comparison then showed the four `queued` rows 7 to 18% slower on x86_64, in three
runs. Each such run compares one build pair at one stack position, with the head always second,
so it cannot separate a cost from layout. We therefore ran the protocol of the next section on
GitHub's runners.

## 8. The protocol

The `Stack sweep` workflow compares a candidate commit against its parent:

- **Runners:** 4 jobs on `ubuntu-latest` (x86_64) and 4 on `ubuntu-24.04-arm` (Neoverse-N2). Each
  job builds both commits with the pinned toolchain and `--locked`, and logs the CPU model and the
  binary hashes.
- **Binaries:** base, candidate, and a copy of the base as an A/A control.
- **Stack positions:** 16 worker-stack shifts, 0 to 3840 in steps of 256, through the shift
  library from the appendix. A probe checks first that the shift moves a new thread's stack by
  exactly that amount.
- **Schedule:** 3 repetitions; within each, the shifts ascend, the rows follow in a fixed order,
  and the binary order rotates by one per repetition.
- **Job figure:** per row, the median of the candidate's 48 runs over the median of the base's,
  minus 1. The A/A figure is the same for the copy.
- **Exclusion:** a job's row is excluded if its A/A figure is off by more than 3%.
- **Verdict per row:** inconclusive if fewer than 3 of the 4 jobs remain, or if the retained
  figures spread by more than 6 points. Otherwise the median decides: below +3% passes, above +5%
  fails, and in between the row gets 4 more jobs. Then the same checks run over all 8 jobs, with
  at least 6 retained, and the median decides at +3%.
- **Precedence:** inconclusive always wins over the median.
- **Scope:** the figure is the median across the 16 sampled positions. The grid can miss narrow
  penalties or hit them systematically, so it says nothing about robustness.

`.github/stack-sweep/report.py` applies these rules and reports each row by CPU model too.

## 9. Results

**The aligned ID (#177).** Medians; a negative change means faster.

| Row | Neoverse-N2 | x86_64 |
|---|---|---|
| `fan_out/workers/4` | fail, +15.1% | inconclusive |
| `fan_out/workers/16` | fail, +20.9% | inconclusive |
| `queued/never/4`, `/16` | pass, -4.2%, -4.0% | fail, +4.9%, +4.2% (8 jobs) |
| `queued/every/4`, `/16` | pass, -2.4%, -1.9% | fail, +5.0%, +4.8% (8 jobs) |
| `ping_pong/pairs/1` | pass, -1.0% | fail, +6.0% |
| `ping_pong/pairs/4` | pass, -3.0% | inconclusive |
| `flood/unbounded`, `/bounded` | pass, +1.1%, -3.6% | inconclusive |

On Neoverse-N2, `fan_out` was slower in every job and at every position. On x86_64 the result
depended on the CPU model: `queued` was +3.4 to +5.2% on EPYC 7763 and +7.3 to +8.8% on EPYC
9V45, with only one or two jobs per model besides the 7763.

**Isolating variants.** Each changes one part of #177 on top of `main`:

- **P** aligns `Incoming`. The items get #177's size and alignment, but the ID stays at offset 1.
- **R** stores the ID inside `ActorRef` in a 16-byte-aligned wrapper. `ActorRef`, `SelfRef`,
  `ActorContext` and the run loop's futures get #177's layout; `Incoming` stays as on `main`.
- **P+R** combines both. It differs from #177 in the ID's offset inside `Incoming` and in the
  types holding an `ActorId` directly, such as `Watcher`.

In the bench build, `ActorRef` and `ActorContext` keep their sizes in every variant; only their
alignment changes. The `+8 bytes` of section 5 apply to builds with the `cluster` feature.

| Row | #177 N2 | P N2 | R N2 | P+R N2 | #177 x86 | P x86 | R x86 | P+R x86 |
|---|---|---|---|---|---|---|---|---|
| `queued/*` | -4.2 to -1.9% | +5.3 to +8.5% | -2.1 to -0.6% | +6.5 to +7.3% | +4 to +5% | +15 to +18% | -0 to +1% | +9 to +10%, spread |
| `fan_out/workers/4` | +15% | +14% | +1% | +15% | spread | +26%, spread | -0% | +10%, spread |
| `fan_out/workers/16` | +21% | +14% | +3% | +13% | spread | +21% | -0% | +12% |
| `flood/unbounded` | +1% | +15% | -1% | +15%, spread | spread | +18% | spread | too few jobs |
| `ping_pong/pairs/1` | -1% | +3% | -0% | +3% | +6% | +8% | +5% | +9% |

The x86_64 CPU mix differed between the variants, which limits comparisons across them. The
variants do not isolate padding from field placement or generated code. Two readings hold: R is
nearly free except on x86 `ping_pong/pairs/1`, and the Neoverse-N2 `fan_out` regression comes with
the change to `Incoming` (P, P+R), not with R.

**Generated code.** Disassembling `main`, #177 and P on aarch64 showed where they differ. P copies
its 32-byte item as two overlapping 16-byte halves (`ldr q0, [x]` and `ldur q1, [x, #15]`) through
unaligned stack slots on every path; `flume::Sender::send` has 48 16-byte accesses in P, 35 in
#177 and 28 in `main`. #177 copies the item aligned but twice as wide. `main` copies the ID
unaligned in four places on the hot path: one in `Mailbox::recv`, three in flume's receive and
send code.

**Tokio's channel (T).** The mailbox over `tokio::sync::mpsc::unbounded_channel`, with the same
items, quota and shared FIFO. Tokio reports the queue length only on the receiver, so T counts the
depth for `ActorContext::queued` in an atomic of its own. Measured locally on aarch64, median over
16 positions and 3 repetitions:

| Row | `main` | T | T without the counter |
|---|---|---|---|
| `queued/never/4` (4 senders) | 5.76 ms | 11.57 ms, +101% | 11.25 ms, +95% |
| `flood/unbounded` (1 sender) | 6.71 ms | 5.98 ms, -11% | 5.37 ms, -20% |

T also showed new slow positions on `flood` in a single 16-byte sweep. We did not measure T further.

**An aligned receive slot (A).** A changes only `Mailbox::recv`: it polls flume's future by hand
into a private `#[repr(align(32))]` slot. `hint::black_box` keeps the slot in memory, since
without it LLVM removes the wrapper and its alignment. The ID then lies inside one 32-byte block
and cannot cross a page. `Incoming`, the items and `ActorRef` stay as on `main`. A local 16-byte
sweep on aarch64, repeat medians in ms, neighbors at ±16 about 5.8 ms in every build:

| Shift | `main` | A | `main` copy |
|---|---|---|---|
| 96 | 8.55 | 6.34 | 8.50 |
| 4064 | 8.36 | 5.88 | 8.38 |
| 208 | 8.18 | 8.35 | 8.18 |
| 512 | 6.37 | 6.76 | 6.44 |
| 944 | 7.46 | 8.05 | 7.56 |

A removes the penalties at 96 and 4064, keeps 208 and makes 512 and 944 about 6 to 7% slower. On
the runners it passed 9 rows on Neoverse-N2, with `flood/bounded` inconclusive. On x86_64 it
failed `flood/unbounded` (+4.8%), `fan_out/workers/4` (+3.7%) and `queued/never/4` (+3.5%), each
over 8 jobs; `ping_pong/pairs/1` and `flood/bounded` were inconclusive, the other rows passed at
+1.6 to +2.9%.

On x86_64, `main`'s `recv` already copies the ID aligned: flume writes the result to `rsp + 0xf`,
so the ID lies at `rsp + 0x10`. A adds work there: a realigned frame, a stack slot for
`black_box` and two more 16-byte moves. It also changes the frame of `flume::Sender::send`, which A
does not touch. Its item temporaries move from 16-byte-aligned offsets to offsets 8 modulo 16.
Either may cause A's cost on x86_64; the assembly does not separate them.

**A on aarch64 only (A64).** A's two statements behind `#[cfg(target_arch = "aarch64")]`, with
`main`'s line otherwise. Built under the same conditions as the reference binaries, A64's `.text`,
`.rodata`, `.eh_frame` and `.gcc_except_table` match A's on aarch64 and `main`'s on x86_64 byte
for byte. The rest differs in one line number in `.data.rel.ro`, LLVM's symbol names and the build
ID. We did not compare section addresses, load layout or relocations, and we did not build A64 on
CI. So the comparison supports, but does not prove, that A's measurements describe A64 on aarch64
and `main`'s behavior describes it on x86_64.

## 10. Decision

`main` stays as it is. #177 fails the tolerance on both runner types. A fails it on x86_64. A64
would avoid A's cost on x86_64 if the code comparison carries over, but we rejected it:

- It removes two stable penalties on aarch64, which is a real but incomplete benefit at specific
  positions of specific binaries. Other positions stay 30 to 40% slower, and two get 6 to 7%
  slower.
- It does not make benchmark comparisons independent of layout.
- It depends on where the compiler places one temporary, which can change with any release.

What we keep is the knowledge in this report and the `Stack sweep` workflow for comparisons that
matter.

This investigation explains why the first benchmark comparison of #174 could not measure the
timers. It does not show that they cost nothing; that is tracked in #178.

Open questions:

- The run-to-run variation at some stack positions (2048 to 2448 on `queued/never/4`) is
  unexplained.
- The stable penalties at 208 and 944 on aarch64 are not assigned to an instruction.
- Why Tokio's channel is twice as slow with four senders is untested; Tokio's cooperative budget
  is one hypothesis.

## Appendix: reproduction

- Machine: aarch64, Linux 6.12, 10 CPUs, 4096-byte pages.
- Compiler: rustc 1.99.0 (b940084d7 2026-09-28) with LLVM 23.1.1. `RUSTC_BOOTSTRAP=1` was used
  only for `-Zprint-type-sizes`.
- Commits: main ceb69f0 and PR #174 at 84eb511 for sections 1 to 3; main 39aec83 as base for
  sections 4 to 6.
- Messaging bench binaries (SHA-256 prefixes): base `75971b17`, aligned `ActorId` `830cf627`,
  aligned `Incoming` `0d339011`.
- Runs: `LD_PRELOAD=stack_shift.so STACK_SHIFT=<n> <bench> --bench --noplot --warm-up-time 0.2
  --measurement-time 0.5 --sample-size 10 '<row>$'`, optionally under `taskset`.
- Variants for sections 7 to 9, each one commit on top of 39aec83: #177 `b7b369b`, P `f8fce53`,
  R `96684f8`, P+R `bc6010f`, A `b548674`, A64 `db42763`, T `ca7c63b`. Only #177 is kept, as the
  head of the closed PR; section 9 describes each of the others.
- Workflow runs: #177 40072083468 and 40074242603; P, R and P+R 40080451549; A 40097044624 and
  40107338617. Their artifacts expire after 90 days.

The shift library, the stack probe and the sweep driver are in `.github/stack-sweep/`. To compare
a commit against its parent on GitHub's runners:

```
gh workflow run stack-sweep.yml -f base=<parent> -f candidate=<commit>
python3 -I .github/stack-sweep/report.py <run ID>
```

For a borderline row, run the workflow again and pass both run IDs to `report.py`. Locally, build
the shift library and the probe as the workflow does, check the probe with
`.github/stack-sweep/probe.sh`, and run `sweep.py run` with three bench binaries.
