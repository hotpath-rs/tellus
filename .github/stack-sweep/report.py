"""Apply the predeclared rules to stack-sweep artifacts.

Usage: report.py RUN_ID [RUN_ID]. Run it inside the repository. The second run ID is the extra run
for borderline rows: it decides only the rows which are borderline in the first run, every other row
keeps its verdict from the first run.
"""

import json
import os
import statistics
import subprocess
import sys
import tempfile

AA_LIMIT = 0.03
SPREAD_LIMIT = 0.06


def verdict(jobs, stage):
    """jobs: (candidate figure, A/A figure) per job. Returns (verdict, median, spread, retained)."""
    jobs = [(round(candidate, 6), round(aa, 6)) for candidate, aa in jobs]
    retained = [candidate for candidate, aa in jobs if abs(aa) <= AA_LIMIT]
    if len(retained) < (3 if stage == 1 else 6):
        return "inconclusive (too few jobs)", None, None, len(retained)
    spread = round(max(retained) - min(retained), 6)
    if spread > SPREAD_LIMIT:
        return "inconclusive (spread)", statistics.median(retained), spread, len(retained)
    median = round(statistics.median(retained), 6)
    if stage == 1:
        result = "pass" if median < 0.03 else "fail" if median > 0.05 else "borderline"
    else:
        result = "pass" if median <= 0.03 else "fail"
    return result, median, spread, len(retained)


def decide(first, second):
    """first, second: (candidate figure, A/A figure) per job of the first and of the extra run, second
    empty without an extra run. Returns (stage, (verdict, median, spread, retained), jobs used)."""
    result = verdict(first, 1)
    if result[0] != "borderline" or not second:
        return 1, result, first
    return 2, verdict(first + second, 2), first + second


def figure(lines, row, name, shift=None):
    def median_of(binary):
        return statistics.median(
            line["median_ns"]
            for line in lines
            if line["row"] == row and line["binary"] == binary and (shift is None or line["shift"] == shift)
        )

    return median_of(name) / median_of("B") - 1


def main(run_ids):
    if len(run_ids) not in (1, 2):
        sys.exit("usage: report.py RUN_ID [RUN_ID]")
    jobs = {}
    with tempfile.TemporaryDirectory() as directory:
        for run_index, run_id in enumerate(run_ids):
            target = os.path.join(directory, run_id)
            subprocess.run(["gh", "run", "download", run_id, "-D", target], check=True)
            for artifact in sorted(os.listdir(target)):
                path = os.path.join(target, artifact)
                metadata = json.load(open(os.path.join(path, "metadata.json")))
                lines = [json.loads(line) for line in open(os.path.join(path, "results.jsonl"))]
                key = (metadata.get("candidate_name", "C"), metadata.get("bench", "messaging"), metadata["runner"])
                jobs.setdefault(key, []).append((run_index, metadata, lines))

    for (candidate, bench, runner), runner_jobs in sorted(jobs.items()):
        first_jobs = [(metadata, lines) for run_index, metadata, lines in runner_jobs if run_index == 0]
        extra_jobs = [(metadata, lines) for run_index, metadata, lines in runner_jobs if run_index == 1]
        print(f"## {candidate}, {bench}, on {runner}, {len(first_jobs)} jobs plus {len(extra_jobs)} for borderline rows")
        for _, metadata, lines in runner_jobs:
            print(f"- run {metadata['run_id']} job {metadata['job']}: {metadata['cpu']}, {len(lines)} lines")
        print()
        print("| Row | Verdict | Median | Spread | Retained | Candidate per job | A/A per job |")
        print("|---|---|---|---|---|---|---|")
        rows = list(dict.fromkeys(line["row"] for line in runner_jobs[0][2]))
        for row in rows:
            first = [(figure(lines, row, "C"), figure(lines, row, "A")) for _, lines in first_jobs]
            extra = [(figure(lines, row, "C"), figure(lines, row, "A")) for _, lines in extra_jobs]
            stage, (result, median, spread, retained), used = decide(first, extra)
            print(
                f"| `{row}` | {result}, stage {stage} | {'' if median is None else f'{median:+.1%}'} | "
                f"{'' if spread is None else f'{spread * 100:.1f} pp'} | {retained}/{len(used)} | "
                + ", ".join(f"{c:+.1%}" for c, _ in used) + " | "
                + ", ".join(f"{a:+.1%}" for _, a in used) + " |"
            )
        print()
        all_jobs = first_jobs + extra_jobs
        cpus = sorted({metadata["cpu"] for metadata, _ in all_jobs})
        print("By CPU model, over all jobs (jobs passing A/A, median candidate figure):")
        print()
        print("| Row | " + " | ".join(cpus) + " |")
        print("|---|" + "---|" * len(cpus))
        for row in rows:
            cells = []
            for cpu in cpus:
                kept = [figure(lines, row, "C") for metadata, lines in all_jobs
                        if metadata["cpu"] == cpu and abs(figure(lines, row, "A")) <= AA_LIMIT]
                cells.append(f"{statistics.median(kept):+.1%} (n={len(kept)})" if kept else "n=0")
            print(f"| `{row}` | " + " | ".join(cells) + " |")
        print()
        shifts = sorted({line["shift"] for line in runner_jobs[0][2]})
        print("Candidate against base per shift (median over all jobs and repetitions):")
        print()
        print("| Row | " + " | ".join(str(s) for s in shifts) + " |")
        print("|---|" + "---|" * len(shifts))
        merged = [line for _, lines in all_jobs for line in lines]
        for row in rows:
            print(f"| `{row}` | " + " | ".join(f"{figure(merged, row, 'C', s):+.0%}" for s in shifts) + " |")
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
