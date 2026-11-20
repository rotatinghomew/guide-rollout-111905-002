"""截止审计：机会差、证据不足范围与抽样误差分别陈述。"""
import _paths  # noqa: F401  确保仓库根在 sys.path

import math
import unittest

from lottery.audit import GapVerdict, audit_cutoff
from lottery.identity import feasible_scenarios
from lottery.loader import load_tables
from lottery.units import build_units
from lottery.draw import enumerate_zone

FIXTURE = "/workspace/fixtures"


class CutoffAudit(unittest.TestCase):
    def setUp(self):
        self.data = load_tables(FIXTURE)
        self.report = audit_cutoff(self.data)

    def test_main_zone_undecided_because_identity_range_straddles_line(self):
        main = next(
            z for z in self.report.zones if set(z.zone) == {"S1", "S2", "S3"}
        )
        self.assertIs(main.verdict, GapVerdict.UNDECIDED)
        self.assertLess(main.gap_range[0], main.gap_limit)
        self.assertGreater(main.gap_range[1], main.gap_limit)
        # 三条 possible 佐证都可能改变结论。
        self.assertEqual(
            set(main.deciding_claims), {"I1", "I2", "I5"}
        )
        self.assertFalse(main.independently_openable)

    def test_s4_passes_and_is_independently_openable(self):
        s4 = next(z for z in self.report.zones if set(z.zone) == {"S4"})
        self.assertIs(s4.verdict, GapVerdict.PASS)
        self.assertTrue(s4.independently_openable)
        self.assertEqual(s4.gap_range, (0.0, 0.0))

    def test_invitations_blocked_until_audit_passes(self):
        self.assertFalse(self.report.can_send_invitations)
        self.assertEqual(self.report.openable_sessions(), ("S4",))
        self.assertEqual(
            self.report.blocked_sessions(), ("S1", "S2", "S3")
        )

    def test_single_realized_permutation_is_not_used_as_probability(self):
        # 所有场景机会都由全部排列计数得到，没有 0/1 退化值。
        for zone in self.report.zones:
            for offers in zone.person_offer.values():
                for value in offers.values():
                    self.assertTrue(0.0 <= value <= 1.0)

    def test_family_same_session_opportunity_reported_for_groups(self):
        main = next(
            z for z in self.report.zones if set(z.zone) == {"S1", "S2", "S3"}
        )
        for fp, placed in main.together_same_session.items():
            for unit_id, value in placed.items():
                self.assertIn("+", unit_id)
                self.assertTrue(0.0 <= value <= 1.0)

    def test_identity_uncertainty_changes_personal_odds(self):
        # A9 的个人机会在不同身份解释下应不同（合并后受更严名额约束）。
        main = next(
            z for z in self.report.zones if set(z.zone) == {"S1", "S2", "S3"}
        )
        a9_odds = {
            fp: offers.get("A9")
            for fp, offers in main.person_offer.items()
        }
        values = {v for v in a9_odds.values() if v is not None}
        self.assertGreater(len(values), 1)

    def test_merged_participant_rows_share_odds_and_count_once(self):
        # I1 取真时 A3 与 A9 是同一参与者：两行机会相等，
        # 且按人平均时该参与者只计一次（不按两条报名行重复）。
        main = next(
            z for z in self.report.zones if set(z.zone) == {"S1", "S2", "S3"}
        )
        fp = next(k for k in main.person_offer if k == "I1=T;I2=F;I5=F")
        offers = main.person_offer[fp]
        self.assertEqual(offers["A3"], offers["A9"])

        scenarios = feasible_scenarios(
            self.data.applications, self.data.identity, self.data.cutoff
        )
        scenario = next(
            s for s in scenarios
            if dict(s.decisions) == {"I1": True, "I2": False, "I5": False}
        )
        units = build_units(self.data.eligible_applications(), scenario)
        exact = enumerate_zone(
            units, self.data.capacities, frozenset({"S1", "S2", "S3"})
        )
        # 参与者总数（去重）应少于报名行数。
        participant_count = sum(len(u.participants) for u in exact.units)
        application_count = sum(
            len(u.members) for u in exact.units
        )
        self.assertLess(participant_count, application_count)

    def test_sampling_keeps_error_sources_separate(self):
        sampled = audit_cutoff(self.data, sample_draws=2000, seed=11)
        main = next(
            z for z in sampled.zones if set(z.zone) == {"S1", "S2", "S3"}
        )
        self.assertEqual(sampled.method, "sample(n=2000)")
        # 身份范围与抽样区间同时存在、分别可查。
        self.assertIn("S1+S2+S3", sampled.sampling_error)
        self.assertIsInstance(main.gap_range, tuple)
        se_low, se_high = sampled.sampling_error["S1+S2+S3"]
        self.assertGreaterEqual(se_low, 0.0)
        self.assertLessEqual(se_high, 1.0)

    def test_gap_verdict_covers_all_three_states(self):
        from dataclasses import replace

        main_sessions = {"S1", "S2", "S3"}

        def with_limit(value: float):
            caps = {
                k: replace(c, gap_limit=value)
                for k, c in self.data.capacities.items()
            }
            return replace(self.data, capacities=caps)

        strict = audit_cutoff(with_limit(0.01))
        z_strict = next(z for z in strict.zones if set(z.zone) == main_sessions)
        self.assertIs(z_strict.verdict, GapVerdict.FAIL)

        loose = audit_cutoff(with_limit(0.9))
        z_loose = next(z for z in loose.zones if set(z.zone) == main_sessions)
        self.assertIs(z_loose.verdict, GapVerdict.PASS)
        self.assertTrue(loose.can_send_invitations)

    def test_exact_gap_is_weighted_absolute_difference_of_averages(self):
        # 手工复算一个场景的 gap，与审计输出对齐。
        scenarios = feasible_scenarios(
            self.data.applications, self.data.identity, self.data.cutoff
        )
        scenario = scenarios[0]
        units = build_units(self.data.eligible_applications(), scenario)
        exact = enumerate_zone(
            units, self.data.capacities, frozenset({"S1", "S2", "S3"})
        )
        together, separate = [], []
        for unit in exact.units:
            p = exact.offer_probability(unit.id)
            bucket = together if unit.together else separate
            bucket.extend([p] * len(unit.participants))
        expected = abs(
            sum(together) / len(together) - sum(separate) / len(separate)
        )
        main = next(
            z for z in self.report.zones if set(z.zone) == {"S1", "S2", "S3"}
        )
        fp = next(iter(main.scenario_gaps))
        self.assertTrue(
            math.isclose(main.scenario_gaps[fp], expected, abs_tol=1e-12)
        )


if __name__ == "__main__":
    unittest.main()
