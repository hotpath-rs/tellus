import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report import decide, verdict


class Verdicts(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(verdict([(0.01, 0.0), (0.0, 0.01), (0.015, -0.01), (0.005, 0.0)], 1)[0], "pass")

    def test_fail(self):
        self.assertEqual(verdict([(0.08, 0.0), (0.07, 0.01), (0.09, -0.01), (0.06, 0.0)], 1)[0], "fail")

    def test_borderline(self):
        self.assertEqual(verdict([(0.03, 0.0), (0.025, 0.0), (0.035, 0.0), (0.03, 0.0)], 1)[0], "borderline")

    def test_stage_one_boundaries(self):
        self.assertEqual(verdict([(0.0299, 0.0)] * 4, 1)[0], "pass")
        self.assertEqual(verdict([(0.03, 0.0)] * 4, 1)[0], "borderline")
        self.assertEqual(verdict([(0.05, 0.0)] * 4, 1)[0], "borderline")
        self.assertEqual(verdict([(0.0501, 0.0)] * 4, 1)[0], "fail")

    def test_excluded_job_is_left_out(self):
        result, median, _, retained = verdict([(0.0, 0.0), (0.01, 0.0), (0.0, 0.0), (0.5, 0.04)], 1)
        self.assertEqual((result, retained), ("pass", 3))
        self.assertEqual(median, 0.0)

    def test_too_few_retained(self):
        self.assertEqual(verdict([(0.0, 0.0), (0.0, 0.0), (0.0, 0.05), (0.0, -0.04)], 1)[0],
                         "inconclusive (too few jobs)")

    def test_spread_beats_passing_median(self):
        self.assertEqual(verdict([(-0.04, 0.0), (0.0, 0.0), (0.0, 0.0), (0.03, 0.0)], 1)[0],
                         "inconclusive (spread)")

    def test_spread_counts_retained_jobs_only(self):
        self.assertEqual(verdict([(0.0, 0.0), (0.01, 0.0), (0.0, 0.0), (0.3, 0.05)], 1)[0], "pass")

    def test_stage_two(self):
        jobs = [(0.025, 0.0)] * 4 + [(0.035, 0.0)] * 4
        self.assertEqual(verdict(jobs, 2)[0], "pass")
        self.assertEqual(verdict([(0.035, 0.0)] * 8, 2)[0], "fail")
        self.assertEqual(verdict([(0.0, 0.0)] * 5 + [(0.0, 0.05)] * 3, 2)[0], "inconclusive (too few jobs)")
        self.assertEqual(verdict([(-0.04, 0.0)] * 4 + [(0.03, 0.0)] * 4, 2)[0], "inconclusive (spread)")



class Stages(unittest.TestCase):
    PASSING = [(0.0, 0.0)] * 4
    FAILING = [(0.10, 0.0)] * 4
    BORDERLINE = [(0.04, 0.0)] * 4

    def test_only_a_borderline_row_is_decided_over_both_runs(self):
        stage, (result, _, _, retained), used = decide(self.BORDERLINE, self.PASSING)
        self.assertEqual((stage, result, retained, len(used)), (2, "pass", 8, 8))

    def test_a_passing_row_keeps_its_verdict_whatever_the_extra_run_shows(self):
        stage, (result, _, _, _), used = decide(self.PASSING, self.FAILING)
        self.assertEqual((stage, result, len(used)), (1, "pass", 4))

    def test_a_failing_row_keeps_its_verdict_whatever_the_extra_run_shows(self):
        stage, (result, _, _, _), used = decide(self.FAILING, self.PASSING)
        self.assertEqual((stage, result, len(used)), (1, "fail", 4))

    def test_an_inconclusive_row_keeps_its_verdict_whatever_the_extra_run_shows(self):
        first = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.05), (0.0, 0.05)]
        stage, (result, _, _, _), _ = decide(first, self.PASSING)
        self.assertEqual((stage, result), (1, "inconclusive (too few jobs)"))

    def test_a_borderline_row_without_an_extra_run_stays_borderline(self):
        stage, (result, _, _, _), _ = decide(self.BORDERLINE, [])
        self.assertEqual((stage, result), (1, "borderline"))


unittest.main()
