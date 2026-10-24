# Actor ID alignment

The messaging benchmarks depend on where a worker thread's stack lies in memory. This page
summarizes why, what we tried and why `main` stays as it is. The [report](report.md) describes
how we got there.

## The problem

`ActorId` wraps a `Uuid`, which is `[u8; 16]` with alignment 1. A mailbox item is an `Incoming<M>`,
which holds either a message or an `ActorId`, so `Incoming<()>` is 17 bytes with alignment 1. The
compiler copies the ID through stack temporaries with unaligned 16-byte loads, for example
`ldur q0, [sp, #13]` in `Mailbox::recv` on aarch64. When such a load crosses a 4 KB page boundary,
it is slow.

Whether it crosses depends on the temporary's address, and with it on where the worker thread's
stack lies. Under our measurement conditions, particular binaries paid a stable penalty at
particular stack positions: 30 to 52% slower in the affected benchmarks, with nearly identical
repeats. An unrelated change can move the temporaries, and with them the positions where this
happens.

That is what misled the first comparison of PR #174 (timers). It showed `queued` 55% slower, and
the stack shift traced that to this copy. Its `flood` result, 24% faster, was not traced. So the
comparison could not measure the timers, and it does not show that they cost nothing: the timer
cost is a separate question, tracked in #178.

## What we tried

Each candidate was measured across worker-stack positions, against `main` 39aec83, with limits we
fixed before measuring. The CI runs used Neoverse-N2 (`ubuntu-24.04-arm`) and AMD EPYC and Intel
Xeon (`ubuntu-latest`).

| Candidate | Result |
|---|---|
| `#[repr(align(16))]` on `ActorId` (#177) | removes the penalty on aarch64, but costs up to +21% on `fan_out` on Neoverse-N2 and +4 to +6% on several x86 rows |
| `#[repr(align(16))]` on `Incoming` | the ID stays unaligned inside it; slower than #177 on most rows |
| An aligned ID inside `ActorRef` only | nearly free, except +5.4% on one x86 row, but leaves the receive copy unaligned |
| Tokio's unbounded channel instead of flume | about twice as slow with four concurrent senders, measured on aarch64 only |
| An aligned receive slot in `Mailbox::recv` (A) | removes two stable penalties on aarch64; +3.5 to +4.8% on three x86 rows |
| The same, on aarch64 only (A64) | same code sections as A on aarch64 and as `main` on x86 in local builds; not measured itself |

A64 was not measured itself. Its code sections match A's on aarch64 and `main`'s on x86 in local
builds, which suggests that A's aarch64 results and `main`'s x86 behavior carry over. We rejected it
anyway:

- It removes two stable penalties on aarch64, but other stack positions stay 30 to 40% slower and
  two of them get 6 to 7% slower.
- It doesn't remove the need to measure across stack positions.
- It depends on where the compiler places one temporary, which can change with any compiler
  release.

## What follows for benchmarks

A comparison of two builds at one stack position can show tens of percent either way that have
nothing to do with the change. The CI comparison in `bench.yml` measures at one position, so a
regression it flags needs confirmation. The `Stack sweep` workflow
(`.github/workflows/stack-sweep.yml`) does that. It compares two commits at 16 worker-stack
positions on both runner types, with an A/A control, and `.github/stack-sweep/report.py` applies
the limits from the [report](report.md#8-the-protocol).
