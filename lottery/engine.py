"""两轮之间的核验事件处理与对外咨询口径。

核验员在两轮之间确认两个家庭号/参与者其实是同一组时：

- 已接受且仍合规的席位守住，不重新抽签；
- 已接受的两份通知互相冲突时，交运营裁决；裁决截至时间之前
  任何一份都不当空位；
- 尚未答复或尚未发出的邀请，按当前可执行的保留范围重核，
  不能重抽一遍后假装原邀请没发过。

咨询接口区分三类口径并做身份脱敏：截止时对公众的机会说明、
已经发给家庭的承诺、现在可以接着发的顺位；回查不暴露其他家庭身份。
"""

from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditReport
from .identity import Scenario, feasible_scenarios
from .ledger import HoldExplanation, Ledger
from .loader import SourceData
from .models import ClaimScope, IdentityClaim, Pool


@dataclass
class Conflict:
    """两份已接受通知在身份合并后产生的冲突。"""

    invitations: tuple[str, str]
    participants: tuple[str, ...]
    reason: str
    adjudicate_by: int | None = None
    resolved: bool = False


@dataclass
class PendingRecheck:
    """未答复邀请按当前保留范围重核的结果（不重抽）。"""

    invitation: str
    still_valid: bool
    detail: str


@dataclass
class HouseholdView:
    """面向单个家庭的脱敏咨询视图。"""

    household: str
    cutoff_explanation: dict[str, object]
    commitments: tuple[dict[str, object], ...]
    next_positions: tuple[dict[str, object], ...]


class OperationsEngine:
    """台账回放 + 核验事件 + 咨询的统一入口。"""

    def __init__(self, data: SourceData):
        self.data = data
        # 截止时刻的可行解释与审计结论（开第一封通知之前的公众口径）。
        self.cutoff_scenarios = feasible_scenarios(
            data.applications, data.identity, data.cutoff
        )
        self.cutoff_audit: AuditReport | None = None
        self.ledger: Ledger | None = None
        self.conflicts: list[Conflict] = []
        self.rechecks: list[PendingRecheck] = []
        self.applied_claims: list[IdentityClaim] = []
        self.now = data.cutoff

    # ------------------------------------------------------------------ #
    # 启动：截止审计 + 台账回放
    # ------------------------------------------------------------------ #
    def run_cutoff_audit(self, **kwargs) -> AuditReport:
        from .audit import audit_cutoff

        self.cutoff_audit = audit_cutoff(self.data, **kwargs)
        return self.cutoff_audit

    def open_ledger(self, scenario: Scenario | None = None, until: int | None = None) -> Ledger:
        """按一种可行解释回放实际通知台账（默认取第一个可行解释）。

        ``until`` 可把台账停在某个核验时刻，使后续身份事件基于当时的
        待回复状态处理，而不是用最终回执倒推。
        """
        scenario = scenario or self.cutoff_scenarios[0]
        self.ledger = Ledger(self.data, scenario)
        self.ledger.replay(until=until)
        self.now = max(
            (n.at for n in self.data.notices if until is None or n.at <= until),
            default=self.now,
        )
        self.ledger.now = self.now
        return self.ledger

    # ------------------------------------------------------------------ #
    # 两轮之间的核验事件
    # ------------------------------------------------------------------ #
    def apply_identity_event(
        self,
        claim: IdentityClaim,
        adjudicate_by: int | None = None,
    ) -> dict[str, object]:
        """处理一条两轮之间到达的确认/反驳佐证。"""
        if self.ledger is None:
            raise RuntimeError("需先 open_ledger 回放台账")
        self.applied_claims.append(claim)
        self.now = max(self.now, claim.received_at)
        self.ledger.now = self.now

        result: dict[str, object] = {
            "claim": claim.id,
            "scope": claim.scope.value,
            "state": claim.state.value,
            "conflicts": [],
            "rechecks": [],
            "preserved": [],
        }

        if claim.scope is ClaimScope.PERSON and claim.state.value == "confirmed":
            conflicts = self._detect_accepted_conflicts(claim)
            conflicts += self._detect_pending_conflicts(claim)
            self.conflicts.extend(conflicts)
            result["conflicts"] = [
                {
                    "invitations": list(c.invitations),
                    "reason": c.reason,
                    "adjudicate_by": adjudicate_by,
                }
                for c in conflicts
            ]
            for conflict in conflicts:
                conflict.adjudicate_by = adjudicate_by
            result["preserved"] = self._preserved_acceptances(claim)

        if claim.scope is ClaimScope.HOUSEHOLD and claim.state.value == "confirmed":
            # 家庭合并不重抽；已接受且合规的席位守住。
            result["preserved"] = self._preserved_acceptances(claim)

        if claim.state.value == "refuted":
            # 反驳只否定“可合并”的假设，不改变已经成立的承诺。
            result["preserved"] = self._preserved_acceptances(claim)

        # 尚未答复/尚未发出的邀请按当前保留范围重核，不重抽。
        result["rechecks"] = self._recheck_pending(claim)
        return result

    def _detect_accepted_conflicts(self, claim: IdentityClaim) -> list[Conflict]:
        """检测同一参与者名下互相冲突的已接受通知。

        台账里冲突通知可能已被运营裁决释放（adjudicate_drop），但核验员
        在两轮之间确认身份时，冲突事实仍须登记在案，因此这里不过滤
        已裁决状态——裁决动作本身就是冲突的处置结果。
        """
        assert self.ledger is not None
        touched_statuses = {"accepted", "adjudicate_drop"}
        relevant = [
            inv
            for inv in self.ledger.invitations.values()
            if inv.status in touched_statuses
        ]
        left_invs = [
            inv for inv in relevant
            if claim.left in self.ledger.unit_by_id[inv.unit_id].members
        ]
        right_invs = [
            inv for inv in relevant
            if claim.right in self.ledger.unit_by_id[inv.unit_id].members
        ]
        conflicts: list[Conflict] = []
        for a in left_invs:
            for b in right_invs:
                if a.notice.id == b.notice.id:
                    continue
                same_session = a.notice.session == b.notice.session
                reason = (
                    "同一参与者在同场持有两份席位通知"
                    if same_session
                    else "同一参与者的多场通知需按更严人次上限复核"
                )
                conflicts.append(
                    Conflict(
                        invitations=(a.notice.id, b.notice.id),
                        participants=(claim.left, claim.right),
                        reason=reason,
                    )
                )
        return conflicts

    def _detect_pending_conflicts(self, claim: IdentityClaim) -> list[Conflict]:
        """两份尚未答复的邀请在身份合并后指向同一参与者：双双保留待裁。"""
        assert self.ledger is not None
        pending = [
            inv
            for inv in self.ledger.invitations.values()
            if inv.status == "pending"
        ]
        left_invs = [
            inv for inv in pending
            if claim.left in self.ledger.unit_by_id[inv.unit_id].members
        ]
        right_invs = [
            inv for inv in pending
            if claim.right in self.ledger.unit_by_id[inv.unit_id].members
        ]
        conflicts: list[Conflict] = []
        for a in left_invs:
            for b in right_invs:
                if a.notice.id == b.notice.id:
                    continue
                conflicts.append(
                    Conflict(
                        invitations=(a.notice.id, b.notice.id),
                        participants=(claim.left, claim.right),
                        reason="两份待回复邀请指向同一参与者，裁决截至前均不得当空位",
                    )
                )
        return conflicts

    def _preserved_acceptances(self, claim: IdentityClaim) -> list[str]:
        """列出合并后仍合规、必须守住的已接受通知。"""
        assert self.ledger is not None
        touched = {claim.left, claim.right}
        preserved: list[str] = []
        for inv in self.ledger.invitations.values():
            if inv.status != "accepted":
                continue
            unit = self.ledger.unit_by_id[inv.unit_id]
            if not (set(unit.members) & touched):
                continue
            if self._acceptance_still_compliant(inv, claim):
                preserved.append(inv.notice.id)
        return preserved

    def _acceptance_still_compliant(
        self, inv, claim: IdentityClaim
    ) -> bool:
        """合并后按共同适用的更严人次上限复核已接受席位。"""
        assert self.ledger is not None
        unit = self.ledger.unit_by_id[inv.unit_id]
        person_groups = list(unit.participants)
        strictest = min(
            min(self.data.applications[aid].limit for aid in pg)
            for pg in person_groups
        )
        # 统计该单元每个参与者当前已接受场次数（合并身份后按更严上限）。
        def accepted_of(member: str) -> set[str]:
            return {
                x.notice.session
                for x in self.ledger.invitations.values()
                if x.status == "accepted"
                and member in self.ledger.unit_by_id[x.unit_id].members
            }

        return all(
            len(accepted_of(member)) <= strictest for member in unit.members
        )

    def _recheck_pending(self, claim: IdentityClaim) -> list[dict[str, object]]:
        """对未答复邀请按当前可执行保留范围重核（保留通知本身，不重抽）。"""
        assert self.ledger is not None
        out: list[dict[str, object]] = []
        for inv in self.ledger.invitations.values():
            if inv.status not in ("pending",):
                continue
            ledger = self.ledger
            cap = self.data.capacities[inv.notice.session]
            state = ledger.seats[inv.notice.session][inv.seat_class]
            still_valid = state.free + inv.headcount >= inv.headcount
            # 身份合并若让两个 pending 通知指向同一参与者，标记冲突待裁。
            if claim.scope is ClaimScope.PERSON:
                sibling = self._pending_sibling(inv, claim)
                if sibling is not None:
                    still_valid = False
                    detail = (
                        f"与 {sibling.notice.id} 指向同一参与者，待运营裁决；"
                        "原邀请保留不重抽"
                    )
                else:
                    detail = "保留范围仍可执行，原邀请继续有效"
            else:
                detail = (
                    "家庭佐证不改变个人席位，原邀请继续有效"
                    if still_valid
                    else "保留范围已变化，需撤回原邀请后按候补顺位续发"
                )
            record = PendingRecheck(inv.notice.id, still_valid, detail)
            self.rechecks.append(record)
            out.append(
                {
                    "invitation": record.invitation,
                    "still_valid": record.still_valid,
                    "detail": record.detail,
                }
            )
        return out

    def _pending_sibling(self, inv, claim: IdentityClaim):
        assert self.ledger is not None
        for other in self.ledger.invitations.values():
            if other is inv or other.status != "pending":
                continue
            members = set(self.ledger.unit_by_id[other.unit_id].members)
            own = set(self.ledger.unit_by_id[inv.unit_id].members)
            if claim.left in own and claim.right in members:
                return other
            if claim.right in own and claim.left in members:
                return other
        return None

    def resolve_conflict(self, conflict: Conflict, drop_invitation: str) -> None:
        """运营裁决：指定释放哪一份；另一份守住。裁决前两份均不得当空位。"""
        if drop_invitation not in conflict.invitations:
            raise ValueError("只能裁决冲突中的通知")
        assert self.ledger is not None
        conflict.resolved = True
        inv = self.ledger.invitations[drop_invitation]
        inv.status = "adjudicate_drop"
        self.ledger._release_hold(inv)

    # ------------------------------------------------------------------ #
    # 咨询口径（脱敏，不暴露其他家庭身份）
    # ------------------------------------------------------------------ #
    def public_cutoff_explanation(self) -> dict[str, object]:
        """截止时对公众作出的机会说明（只含分区级聚合，不含任何家庭标识）。"""
        if self.cutoff_audit is None:
            self.run_cutoff_audit()
        audit = self.cutoff_audit
        return {
            "method": audit.method,
            "zones": [
                {
                    "sessions": sorted(zone.zone),
                    "gap_limit": zone.gap_limit,
                    "gap_range_identity_uncertainty": [
                        round(zone.gap_range[0], 4),
                        round(zone.gap_range[1], 4),
                    ],
                    "verdict": zone.verdict.value,
                    "sampling_error": audit.sampling_error.get(
                        "+".join(sorted(zone.zone))
                    ),
                    "openable_independently": zone.independently_openable,
                }
                for zone in audit.zones
            ],
            "can_send_invitations": audit.can_send_invitations,
        }

    def household_consultation(self, household: str) -> HouseholdView:
        """单个家庭回查：自己的机会、承诺与当前顺位，看不到其他家庭。"""
        if self.ledger is None:
            self.open_ledger()
        assert self.ledger is not None

        own_rows = [
            aid
            for aid, app in self.data.applications.items()
            if app.household == household
        ]
        own_units = {
            self.ledger.unit_by_member[aid].id
            for aid in own_rows
            if aid in self.ledger.unit_by_member
        }

        commitments = []
        for inv in self.ledger.invitations.values():
            if inv.unit_id not in own_units:
                continue
            commitments.append(
                {
                    "session": inv.notice.session,
                    "status": inv.status,
                    "seat_class": inv.seat_class.value,
                    "round": inv.round_no,
                    "expires": inv.notice.expires,
                }
            )

        positions = []
        for session, entries in self.ledger._waitlists.items():
            for entry in entries:
                if entry.unit.id not in own_units:
                    continue
                positions.append(
                    {
                        "session": session,
                        "rank": entry.rank,
                        "active": entry.active,
                        "skip_reason": entry.skip_reason,
                    }
                )

        cutoff_view = self._household_cutoff_odds(own_units)
        return HouseholdView(
            household=household,
            cutoff_explanation=cutoff_view,
            commitments=tuple(commitments),
            next_positions=tuple(positions),
        )

    def _household_cutoff_odds(self, own_units: set[str]) -> dict[str, object]:
        if self.cutoff_audit is None:
            self.run_cutoff_audit()
        audit = self.cutoff_audit
        odds: dict[str, list[float]] = {}
        same_session: dict[str, list[float]] = {}
        for zone in audit.zones:
            for fp, offers in zone.person_offer.items():
                for unit_id in own_units:
                    # person_offer 以参与者首报名行标识；用单元成员匹配。
                    for person_key, value in offers.items():
                        if person_key in unit_id.split("+") or unit_id.startswith(
                            person_key
                        ):
                            odds.setdefault(unit_id, []).append(value)
            for fp, placed in zone.together_same_session.items():
                for unit_id, value in placed.items():
                    if unit_id in own_units:
                        same_session.setdefault(unit_id, []).append(value)
        return {
            "personal_offer_range": {
                uid: [round(min(v), 4), round(max(v), 4)]
                for uid, v in odds.items()
            },
            "family_same_session_range": {
                uid: [round(min(v), 4), round(max(v), 4)]
                for uid, v in same_session.items()
            },
        }

    def explain_vacancy(self, session: str, seat_class: Pool) -> HoldExplanation:
        """工作人员逐笔回查：这笔空位为何还不能补发。"""
        if self.ledger is None:
            self.open_ledger()
        assert self.ledger is not None
        return self.ledger.explain_hold(session, seat_class)

    def explain_limit_block(self, member_application: str) -> dict[str, object]:
        """回查哪一项承诺把原本可用的递补变成了违反参与上限。"""
        if self.ledger is None:
            self.open_ledger()
        assert self.ledger is not None
        unit = self.ledger.unit_by_member.get(member_application)
        if unit is None:
            return {"application": member_application, "blockers": []}
        accepted = [
            inv
            for inv in self.ledger.invitations.values()
            if inv.status == "accepted"
            and member_application
            in self.ledger.unit_by_id[inv.unit_id].members
        ]
        limit = unit.per_person_limit
        return {
            "application": member_application,
            "unit": unit.id,
            "per_person_limit": limit,
            "accepted_count": len(accepted),
            "would_violate": len(accepted) >= limit,
            "blocking_commitments": [
                {
                    "invitation": inv.notice.id,
                    "session": inv.notice.session,
                    "seat_class": inv.seat_class.value,
                }
                for inv in accepted
            ],
        }

    # ------------------------------------------------------------------ #
    # 身份佐证使用一致性：同一条佐证不许一个场次合并、另一个场次拆开
    # ------------------------------------------------------------------ #
    def identity_use_by_session(self) -> dict[str, dict[str, str]]:
        """按场次汇总每条已定性人证在通知台账中的实际使用方式。

        返回 ``session -> claim_id -> merged|split|untouched``。
        若一条已确认人证涉及的两个报名行在某场以同一单元出现记为
        ``merged``，以不同单元各占席位记为 ``split``。
        """
        if self.ledger is None:
            self.open_ledger()
        assert self.ledger is not None
        usage: dict[str, dict[str, str]] = {}
        person_claims = [
            c
            for c in self.data.identity
            if c.scope is ClaimScope.PERSON and c.state.value == "confirmed"
        ]
        for inv in self.ledger.invitations.values():
            session = inv.notice.session
            row_set = set(self.ledger.unit_by_id[inv.unit_id].members)
            for claim in person_claims:
                pair = {claim.left, claim.right}
                if not (pair & row_set):
                    continue
                if pair <= row_set:
                    flag = "merged"
                else:
                    flag = "split"
                prior = usage.setdefault(session, {}).get(claim.id)
                if prior is not None and prior != flag:
                    # 同一场内自相矛盾也属于不一致。
                    usage[session][claim.id] = "inconsistent"
                else:
                    usage.setdefault(session, {})[claim.id] = flag
        return usage

    def identity_consistency_violations(self) -> list[dict[str, object]]:
        """列出身份佐证被不一致或重复发号使用的情形。

        两种违规都报：

        - 同一条已确认人证在一个场次以合并单元出现、另一个场次被拆开
          （跨场 merged/split 混用）；
        - 已确认是同一参与者的两个报名行，在任一场次被当作两个独立
          单元各占席位（重复发号），即便各场都“一致地拆开”也算。
        """
        usage = self.identity_use_by_session()
        claims = {
            c.id: c
            for c in self.data.identity
            if c.scope is ClaimScope.PERSON and c.state.value == "confirmed"
        }
        flags_by_claim: dict[str, dict[str, str]] = {}
        for session, per_claim in usage.items():
            for claim_id, flag in per_claim.items():
                flags_by_claim.setdefault(claim_id, {})[session] = flag
        violations: list[dict[str, object]] = []
        for claim_id, per_session in flags_by_claim.items():
            effective = {f for f in per_session.values() if f != "untouched"}
            cross_session_mix = (
                "inconsistent" in effective or len(effective) > 1
            )
            duplicate_issue = "split" in effective
            if cross_session_mix or duplicate_issue:
                violations.append(
                    {
                        "claim": claim_id,
                        "endpoints": sorted(
                            (claims[claim_id].left, claims[claim_id].right)
                        ),
                        "usage_by_session": per_session,
                        "cross_session_mix": cross_session_mix,
                        "duplicate_separate_units": duplicate_issue,
                        "detail": (
                            "同一条佐证在不同场次被合并/拆开混用"
                            if cross_session_mix
                            else "已确认同一参与者被拆成两个独立单元重复发号"
                        ),
                    }
                )
        return violations
