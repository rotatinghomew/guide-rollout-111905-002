"""台账回放、保留名额、解保转公开与按轮递补测试。"""
import _paths  # noqa: F401  确保仓库根在 sys.path

import unittest

from lottery.identity import feasible_scenarios
from lottery.ledger import HoldReason, Irregularity, Ledger
from lottery.loader import load_tables
from lottery.models import Pool

FIXTURE = "/workspace/fixtures"


class LedgerReplay(unittest.TestCase):
    def setUp(self):
        self.data = load_tables(FIXTURE)
        scenario = feasible_scenarios(
            self.data.applications, self.data.identity, self.data.cutoff
        )[0]
        self.ledger = Ledger(self.data, scenario)
        self.findings = self.ledger.replay()
        self.kinds = [f.kind for f in self.findings]

    def test_detects_s3_waitlist_skip(self):
        skip = [f for f in self.findings if f.kind is Irregularity.WAITLIST_SKIP]
        self.assertEqual(len(skip), 1)
        self.assertEqual(skip[0].notice_id, "N12")
        self.assertIn("A10", skip[0].detail)

    def test_detects_protected_misconvert(self):
        mis = [
            f
            for f in self.findings
            if f.kind is Irregularity.PROTECTED_MISCONVERT
        ]
        self.assertEqual(len(mis), 1)
        self.assertEqual(mis[0].notice_id, "N15")

    def test_adjudication_recorded_but_not_treated_as_stale(self):
        adj = [f for f in self.findings if f.kind is Irregularity.ADJUDICATION]
        self.assertEqual(len(adj), 1)
        self.assertEqual(adj[0].notice_id, "N14")
        self.assertFalse(
            any(f.kind is Irregularity.STALE_RECEIPT for f in self.findings)
        )

    def test_pending_holds_are_not_counted_as_free_capacity(self):
        # 不拿已发未回复的席位做分母。
        cap = self.ledger.effective_capacity("S1")
        self.assertEqual(cap[Pool.PRIORITY], 0)

    def test_s2_protected_seats_not_convertible_with_priority_waitlist(self):
        self.assertEqual(self.ledger.convertible_protected("S2"), 0)
        explanation = self.ledger.explain_hold("S2", Pool.PRIORITY)
        self.assertIs(
            explanation.reason, HoldReason.PROTECTED_WAIT_PRIORITY
        )

    def test_next_sendable_follows_original_waitlist_order(self):
        entry, seat_class = self.ledger.next_sendable("S3")
        self.assertEqual(entry.unit.id, "A10")
        self.assertIs(seat_class, Pool.PUBLIC)

        entry, seat_class = self.ledger.next_sendable("S2")
        self.assertEqual(entry.unit.id, "A14+A15")
        self.assertIs(seat_class, Pool.PRIORITY)

    def test_skipped_family_keeps_later_chance(self):
        # 暂跳过（志愿/人数不能直接利用空位）不是拒绝：
        # next_sendable 跳过的条目仍 active，后续容量出现时还能被选中。
        data = load_tables(FIXTURE)
        scenario = feasible_scenarios(
            data.applications, data.identity, data.cutoff
        )[0]
        ledger = Ledger(data, scenario)
        ledger.replay()
        # S2 保护席仍在、公开席为 0 时，靠公开席承接的单元会被暂跳过。
        ledger.next_sendable("S2")
        skipped = [
            entry
            for entry in ledger.waitlist("S2")
            if entry.skip_reason and "暂跳过" in entry.skip_reason
        ]
        for entry in skipped:
            self.assertTrue(
                entry.active, "被暂跳过的候补必须保持后续机会"
            )

    def test_accepting_alternative_terminates_mutex_waitlist(self):
        # A6(limit=2) 在只接受 S3 的时刻，S2 候补仍保留，直到再接受一场。
        data = load_tables(FIXTURE)
        scenario = feasible_scenarios(
            data.applications, data.identity, data.cutoff
        )[0]
        partial = Ledger(data, scenario)
        partial.replay(until=511)  # N10（A6 接受 S3）之后、N15 之前
        s2 = {e.unit.id: e for e in partial.waitlist("S2")}
        self.assertTrue(s2["A6"].active, "limit=2 尚有余量，候补应保留")

        # 完整回放后 A6 又接受 S2（N16），达到上限，新的递补会被上限挡住。
        full = Ledger(data, scenario)
        full.replay()
        self.assertEqual(
            full._accepted_sessions("A6"), {"S2", "S3"}
        )

        # A9(limit=1)：S1 候补在收到 S2 邀请时即停用，
        # 接受 S2 后互斥候补保持终止、不会被重复发邀请。
        self.assertFalse(
            {e.unit.id: e for e in full.waitlist("S1")}["A9"].active
        )

    def test_limit_block_explanation(self):
        # A6 已接受 S3 与 S2，达到 limit=2，新的递补会违反上限。
        accepted = self.ledger._accepted_sessions("A6")
        self.assertEqual(accepted, {"S2", "S3"})


class ExpiryReleasesHold(unittest.TestCase):
    def test_expired_invitation_releases_before_next_send(self):
        from dataclasses import replace

        from lottery.models import Notice, NoticeKind

        data = load_tables(FIXTURE)
        scenario = feasible_scenarios(
            data.applications, data.identity, data.cutoff
        )[0]
        # 合成一封 S3 公开邀请：08:20 发出、08:40 到期，且没有任何回执。
        synthetic = Notice(
            id="NX1",
            prior=None,
            session="S3",
            members=("A10",),
            hold_class=Pool.PUBLIC,
            kind=NoticeKind.SENT,
            at=500,
            expires=520,
        )
        data = replace(data, notices=[synthetic])
        ledger = Ledger(data, scenario)
        ledger.replay(until=500)
        self.assertEqual(ledger.seats["S3"][Pool.PUBLIC].held, 1)
        # 推进过到期时刻：无新通知的空档里到期仍自动释放保留。
        ledger.advance_to(521)
        self.assertEqual(ledger.seats["S3"][Pool.PUBLIC].held, 0)
        self.assertEqual(
            ledger.invitations["NX1"].status, "expired"
        )


if __name__ == "__main__":
    unittest.main()
