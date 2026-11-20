"""身份佐证的可行解释与时序口径测试。"""
import _paths  # noqa: F401  确保仓库根在 sys.path

import unittest

from lottery.identity import (
    build_state,
    feasible_scenarios,
)
from lottery.loader import load_tables
from lottery.models import ClaimState

FIXTURE = "/workspace/fixtures"


class IdentityTiming(unittest.TestCase):
    def setUp(self):
        self.data = load_tables(FIXTURE)

    def test_cutoff_keeps_possible_claims_alive(self):
        # 08:00 截止时 I1/I2/I5 已到达且尚未被取代，全部仍是 possible。
        state = build_state(self.data.identity, 480)
        self.assertEqual(
            {c.id for c in state.possible_person}, {"I1", "I2"}
        )
        self.assertEqual(
            {c.id for c in state.possible_household}, {"I5"}
        )

    def test_supersession_only_takes_effect_when_superseder_arrives(self):
        # 07:55 时 I5 仍有效；09:12 I6 到达后 I5 被反驳取代。
        early = build_state(self.data.identity, 475)
        self.assertIn("I5", {c.id for c in early.possible_household})
        later = build_state(self.data.identity, 552)
        self.assertNotIn("I5", {c.id for c in later.possible_household})
        self.assertEqual(later.refuted_household, (("A1", "A14"),))

    def test_eight_feasible_scenarios_at_cutoff(self):
        scenarios = feasible_scenarios(
            self.data.applications, self.data.identity, 480
        )
        # 三条独立 possible，2^3 = 8 种解释，且全部与已知事实相容。
        self.assertEqual(len(scenarios), 8)
        decisions = {frozenset(s.decisions) for s in scenarios}
        self.assertEqual(len(decisions), 8)

    def test_refuted_edge_makes_merging_scenario_infeasible(self):
        # 09:08 后 I3 确认 A3=A9、I4 反驳 A9=A13：
        # 任何把 A13 并入 A9/A13 同组的解释都不可行。
        scenarios = feasible_scenarios(
            self.data.applications, self.data.identity, 548
        )
        for scenario in scenarios:
            g9 = scenario.participant_of("A9")
            g13 = scenario.participant_of("A13")
            self.assertNotEqual(g9, g13)
            self.assertEqual(
                scenario.participant_of("A3"),
                scenario.participant_of("A9"),
            )

    def test_household_refutation_keeps_groups_separate(self):
        scenarios = feasible_scenarios(
            self.data.applications, self.data.identity, 552
        )
        for scenario in scenarios:
            self.assertNotEqual(
                scenario.household_of("A1"),
                scenario.household_of("A14"),
            )


if __name__ == "__main__":
    unittest.main()
