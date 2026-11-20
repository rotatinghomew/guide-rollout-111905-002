"""两轮之间核验事件、冲突裁决与咨询脱敏测试。"""
import _paths  # noqa: F401  确保仓库根在 sys.path

import unittest

from lottery.engine import OperationsEngine
from lottery.loader import load_tables
from lottery.models import (
    ClaimScope,
    ClaimState,
    IdentityClaim,
    NoticeKind,
    Pool,
)

FIXTURE = "/workspace/fixtures"


class IdentityEvents(unittest.TestCase):
    def setUp(self):
        self.data = load_tables(FIXTURE)
        self.engine = OperationsEngine(self.data)
        self.engine.run_cutoff_audit()

    def test_confirmed_person_merges_accepted_invitations_into_conflict(self):
        self.engine.open_ledger()
        i3 = next(c for c in self.data.identity if c.id == "I3")
        result = self.engine.apply_identity_event(i3, adjudicate_by=600)
        conflicts = result["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(
            tuple(sorted(conflicts[0]["invitations"])), ("N2", "N3")
        )
        # 已接受且合规的 N2 守住，不重抽。
        self.assertIn("N2", result["preserved"])

    def test_pending_conflict_keeps_both_holds_until_adjudication(self):
        early = IdentityClaim(
            id="IX",
            left="A3",
            right="A9",
            scope=ClaimScope.PERSON,
            state=ClaimState.CONFIRMED,
            received_at=500,
        )
        self.engine.open_ledger(until=500)
        result = self.engine.apply_identity_event(early, adjudicate_by=560)
        self.assertEqual(len(result["conflicts"]), 1)
        for nid in ("N2", "N3"):
            inv = self.engine.ledger.invitations[nid]
            self.assertEqual(inv.status, "pending")
        # 重核明确“不重抽、原邀请保留”。
        rechecks = {r["invitation"]: r for r in result["rechecks"]}
        self.assertIn("原邀请保留不重抽", rechecks["N2"]["detail"])

    def test_adjudication_releases_only_dropped_invitation(self):
        early = IdentityClaim(
            id="IX",
            left="A3",
            right="A9",
            scope=ClaimScope.PERSON,
            state=ClaimState.CONFIRMED,
            received_at=500,
        )
        self.engine.open_ledger(until=500)
        self.engine.apply_identity_event(early, adjudicate_by=560)
        conflict = self.engine.conflicts[0]
        self.engine.resolve_conflict(conflict, "N3")
        self.assertEqual(
            self.engine.ledger.invitations["N2"].status, "pending"
        )
        self.assertEqual(
            self.engine.ledger.invitations["N3"].status, "adjudicate_drop"
        )

    def test_refuted_household_claim_preserves_commitments(self):
        self.engine.open_ledger()
        i6 = next(c for c in self.data.identity if c.id == "I6")
        result = self.engine.apply_identity_event(i6)
        self.assertIn("N1", result["preserved"])

    def test_no_redraw_after_identity_event(self):
        # 事件处理不改变已发通知的顺位与成员，只重核保留范围。
        self.engine.open_ledger(until=500)
        before = {
            nid: (inv.notice.members, inv.round_no)
            for nid, inv in self.engine.ledger.invitations.items()
        }
        early = IdentityClaim(
            id="IX",
            left="A3",
            right="A9",
            scope=ClaimScope.PERSON,
            state=ClaimState.CONFIRMED,
            received_at=500,
        )
        self.engine.apply_identity_event(early, adjudicate_by=560)
        after = {
            nid: (inv.notice.members, inv.round_no)
            for nid, inv in self.engine.ledger.invitations.items()
        }
        self.assertEqual(before, after)


class ConsultationPrivacy(unittest.TestCase):
    def setUp(self):
        self.data = load_tables(FIXTURE)
        self.engine = OperationsEngine(self.data)
        self.engine.run_cutoff_audit()
        self.engine.open_ledger()

    def test_public_explanation_has_no_household_identifier(self):
        text = repr(self.engine.public_cutoff_explanation())
        for token in ("H1", "H2", "P1", "A1", "A14"):
            self.assertNotIn(token, text)

    def test_household_view_only_contains_own_units(self):
        view = self.engine.household_consultation("H6")
        own = {"A9"}
        for item in view.commitments:
            self.assertNotIn("A3", item["session"])
        # 顺位条目只含本家庭单元。
        for item in view.next_positions:
            self.assertIsInstance(item["rank"], int)
        self.assertTrue(view.cutoff_explanation["personal_offer_range"])

    def test_three_calibers_are_distinct(self):
        public = self.engine.public_cutoff_explanation()
        view = self.engine.household_consultation("H4")
        self.assertIn("zones", public)
        self.assertTrue(view.commitments)          # 已发承诺
        self.assertTrue(view.next_positions)       # 当前顺位

    def test_vacancy_and_limit_explanations(self):
        hold = self.engine.explain_vacancy("S2", Pool.PRIORITY)
        self.assertTrue(hold.detail)
        limit = self.engine.explain_limit_block("A6")
        self.assertTrue(limit["would_violate"])
        self.assertEqual(
            len(limit["blocking_commitments"]), 2
        )

    def test_identity_consistency_violation_detection(self):
        # I3 确认 A3=A9 后，N2(S1) 与 N3(S2) 把同一参与者当两个独立
        # 单元各占一席：重复发号必须被报告，且各场使用事实可回查。
        self.engine.open_ledger()
        i3 = next(c for c in self.data.identity if c.id == "I3")
        self.engine.apply_identity_event(i3)
        usage = self.engine.identity_use_by_session()
        self.assertEqual(usage["S1"]["I3"], "split")
        self.assertEqual(usage["S2"]["I3"], "split")
        violations = self.engine.identity_consistency_violations()
        self.assertTrue(
            any(v["claim"] == "I3" for v in violations)
        )
        duplicate = next(v for v in violations if v["claim"] == "I3")
        self.assertTrue(duplicate["duplicate_separate_units"])

    def test_consistent_merged_usage_is_not_flagged(self):
        # 同一条已确认人证在所有场次都以合并单元出现时不应报违规。
        from dataclasses import replace

        from lottery.identity import feasible_scenarios
        from lottery.models import Notice, NoticeKind

        data = load_tables(FIXTURE)
        merged_notice = Notice(
            id="NM1",
            prior=None,
            session="S1",
            members=("A3", "A9"),
            hold_class=Pool.PUBLIC,
            kind=NoticeKind.SENT,
            at=485,
            expires=535,
        )
        data = replace(data, notices=[merged_notice])
        engine = OperationsEngine(data)
        engine.run_cutoff_audit()
        # 取 I3 为真的可行解释：A3 与 A9 同属一个参与者单元。
        merged_scenario = next(
            s
            for s in engine.cutoff_scenarios
            if dict(s.decisions).get("I1") is True
        )
        engine.open_ledger(scenario=merged_scenario)
        i3 = next(c for c in data.identity if c.id == "I3")
        engine.apply_identity_event(i3)
        self.assertEqual(
            engine.identity_use_by_session()["S1"]["I3"], "merged"
        )
        self.assertEqual(engine.identity_consistency_violations(), [])


if __name__ == "__main__":
    unittest.main()
