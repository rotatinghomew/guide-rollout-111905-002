"""通知台账回放、保留名额与按轮递补。

核心口径：

- 整组邀请占整组可用名额；到期之前，被保留的席位不能同时作为空位
  递给下一户（不能拿已发未回复的席位做分母）。
- 接受一个可替代志愿会终止其互斥候补，但不会取消同一家庭已经承诺、
  且依规则可以同时参加的其他成员。
- 保护席解除时，只把“实际空着且没有可递补优先单元”的保护席转公开；
  转换用的是实际剩余容量与仍有效的候补。
- 候补顺序遵照原轮规则；志愿或人数并非可直接利用的空位时，能暂跳过的
  家庭保持后续机会，跳过不等于拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .identity import Scenario
from .loader import OrderRow, SourceData
from .models import Notice, NoticeKind, Pool
from .units import DrawUnit, build_units


class HoldReason(str, Enum):
    """一笔空位此刻还不能补发的原因（供工作人员逐笔回查）。"""

    PENDING_INVITATION = "pending_invitation"     # 已发未回复，保留中
    PROTECTED_NOT_RELEASED = "protected_not_released"  # 未到解除时刻
    PROTECTED_WAIT_PRIORITY = "protected_wait_priority"  # 解保但仍有优先候补
    NO_ELIGIBLE_WAITLIST = "no_eligible_waitlist"  # 候补都不能直接利用
    FILLED = "filled"


class Irregularity(str, Enum):
    """台账回放中发现的口径偏差（只报告，不静默改写历史）。"""

    RESEND_WITHOUT_NEW_ROUND = "resend_without_new_round"
    # 上一邀请仍在保留期内又向同场发新邀请
    OVERSEND_DURING_HOLD = "oversend_during_hold"
    # 双人/整组报名被两个单人空位抵掉
    SPLIT_ATOMIC_GROUP = "split_atomic_group"
    # 跳过仍有效、且可直接利用空位的靠前候补
    WAITLIST_SKIP = "waitlist_skip"
    # 把保护席在有优先候补时转作公开
    PROTECTED_MISCONVERT = "protected_misconvert"
    # 通知成员与已知抽签单元对不上
    UNKNOWN_MEMBERS = "unknown_members"
    # 同一条身份佐证在一个场次合并、另一个场次拆开
    INCONSISTENT_IDENTITY_USE = "inconsistent_identity_use"
    # 回执引用了不存在或已终结的邀请
    STALE_RECEIPT = "stale_receipt"
    # 裁决释放已接受邀请：冲突须交运营裁决，裁决前占位仍有效
    ADJUDICATION = "adjudication"


@dataclass
class SeatState:
    """一场一席类的实时账。"""

    total: int
    held: int = 0          # 已发未回复（含未裁决占位）保留数
    committed: int = 0     # 已接受确定占用数

    @property
    def free(self) -> int:
        return self.total - self.held - self.committed


@dataclass
class InvitationState:
    """一封邀请在台账中的运行态。"""

    notice: Notice
    round_no: int
    status: str                          # pending/accepted/declined/expired/adjudicating
    unit_id: str
    headcount: int
    seat_class: Pool


@dataclass
class WaitEntry:
    """候补顺位条目（顺序取自 order.csv 原轮规则）。"""

    rank: int
    unit: DrawUnit
    active: bool = True
    skip_reason: str | None = None      # 暂跳过原因；保持后续机会


@dataclass
class ReplayFinding:
    at: int
    kind: Irregularity
    notice_id: str | None
    detail: str


@dataclass
class HoldExplanation:
    """一笔空位为何不能补发的可回查解释。"""

    session: str
    seat_class: Pool
    free_seats: int
    reason: HoldReason
    blocking_notice: str | None
    detail: str


class Ledger:
    """按时间顺序回放通知台账，并维护席位与候补状态。"""

    def __init__(self, data: SourceData, scenario: Scenario):
        self.data = data
        self.scenario = scenario
        self.units = build_units(data.eligible_applications(), scenario)
        self.unit_by_member: dict[str, DrawUnit] = {}
        for unit in self.units:
            for member in unit.members:
                self.unit_by_member[member] = unit
        self.unit_by_id: dict[str, DrawUnit] = {u.id: u for u in self.units}

        # 席位账：session -> 席类 -> SeatState
        self.seats: dict[str, dict[Pool, SeatState]] = {}
        for sid, cap in data.capacities.items():
            self.seats[sid] = {
                Pool.PRIORITY: SeatState(cap.protected),
                Pool.PUBLIC: SeatState(cap.public_seats),
            }

        self.invitations: dict[str, InvitationState] = {}
        self.findings: list[ReplayFinding] = []
        self.now: int = 0
        self._round_counter = 0
        self._replay_cursor = 0
        self._ordered_notices = sorted(
            data.notices, key=lambda n: (n.at, n.id)
        )
        self._waitlists = self._build_waitlists()

    # ------------------------------------------------------------------ #
    # 候补顺序：order.csv 原轮人工顺位；同场 rank 升序。
    # ------------------------------------------------------------------ #
    def _build_waitlists(self) -> dict[str, list[WaitEntry]]:
        rows_by_session: dict[str, list[OrderRow]] = {}
        for row in self.data.order:
            rows_by_session.setdefault(row.session, []).append(row)
        waitlists: dict[str, list[WaitEntry]] = {}
        for session, rows in rows_by_session.items():
            rows.sort(key=lambda r: r.rank)
            entries: list[WaitEntry] = []
            seen_units: set[str] = set()
            for row in rows:
                unit = self.unit_by_member.get(row.application)
                if unit is None or unit.id in seen_units:
                    continue
                seen_units.add(unit.id)
                entries.append(WaitEntry(rank=row.rank, unit=unit))
            waitlists[session] = entries
        return waitlists

    def waitlist(self, session: str) -> list[WaitEntry]:
        return self._waitlists.get(session, [])

    # ------------------------------------------------------------------ #
    # 回放
    # ------------------------------------------------------------------ #
    def replay(self, until: int | None = None) -> list[ReplayFinding]:
        """按时间顺序回放通知台账。

        ``until`` 给定时只处理该时刻（含）之前、且尚未回放的通知，
        用于表达两轮之间某个核验事件发生时的运行态；可重复调用续放，
        同一封通知不会重复入账。到期处理同样不超过当前时刻。
        """
        while self._replay_cursor < len(self._ordered_notices):
            notice = self._ordered_notices[self._replay_cursor]
            if until is not None and notice.at > until:
                break
            self._replay_cursor += 1
            self.now = notice.at
            self._expire_due(notice.at)
            if notice.is_invitation:
                self._apply_invitation(notice)
            else:
                self._apply_receipt(notice)
        if until is not None:
            self.now = until
        return self.findings

    def advance_to(self, at: int) -> list[ReplayFinding]:
        """把台账时钟推进到 ``at``，处理此刻之前的到期与已到通知。

        没有新通知的时间空档也能用它触发到期释放，使“到期之前不能
        递补、到期之后空位可发”的口径可被逐笔核验。
        """
        if at < self.now:
            raise ValueError("台账时钟只能向前推进")
        self.replay(until=at)
        self._expire_due(at)
        self.now = at
        return self.findings

    def _expire_due(self, at: int) -> None:
        for inv in self.invitations.values():
            if (
                inv.status == "pending"
                and inv.notice.expires is not None
                and inv.notice.expires <= at
            ):
                inv.status = "expired"
                self._release_hold(inv)

    def _apply_invitation(self, notice: Notice) -> None:
        unit = self._resolve_unit(notice)
        if unit is None:
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.UNKNOWN_MEMBERS,
                    notice.id,
                    f"通知成员 {notice.members} 无法对应已知抽签单元",
                )
            )
            return

        # 整组邀请必须整单元发，禁止用两个单人空位抵掉双人报名。
        if unit.together and set(notice.members) != set(unit.members):
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.SPLIT_ATOMIC_GROUP,
                    notice.id,
                    f"整组单元 {unit.id} 被拆开发送: {notice.members}",
                )
            )

        round_no = self._classify_round(notice)
        cap = self.data.capacities[notice.session]
        seat_class = notice.hold_class

        # 已发未回复的席位不能再发：检查本场本席类是否有容量且无保留冲突。
        state = self.seats[notice.session][seat_class]
        headcount = len(notice.members)
        if state.free < headcount:
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.OVERSEND_DURING_HOLD,
                    notice.id,
                    f"{notice.session} {seat_class.value} 无可用空位（保留中），仍发邀请",
                )
            )

        # 候补顺位检查：是否跳过了仍有效、且能直接利用“同席类”空位的靠前单元。
        skip = self._check_waitlist_skip(notice, unit, headcount, seat_class)
        if skip:
            self.findings.append(skip)

        # 保护席转公开检查。
        if (
            seat_class is Pool.PUBLIC
            and self.seats[notice.session][Pool.PRIORITY].free > 0
            and notice.at >= cap.release_at
            and self._has_priority_waitlist(notice.session)
        ):
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.PROTECTED_MISCONVERT,
                    notice.id,
                    f"{notice.session} 仍有优先候补，保护席不得转公开",
                )
            )

        state.held += headcount
        self.invitations[notice.id] = InvitationState(
            notice=notice,
            round_no=round_no,
            status="pending",
            unit_id=unit.id,
            headcount=headcount,
            seat_class=seat_class,
        )
        self._deactivate_waitlist(unit, notice.session)

    def _classify_round(self, notice: Notice) -> int:
        """按场划分邀请轮次。

        一场的第一封邀请是第 1 轮；当该场此前的邀请都已离开待复状态
        （接受/拒绝/到期/裁决）后再发的新邀请进入下一轮。待复期间发出的
        邀请属于同一轮的并行通知，轮次号不递增。
        """
        session_history = [
            inv
            for inv in self.invitations.values()
            if inv.notice.session == notice.session
        ]
        if not session_history:
            self._round_counter = 1
            return 1
        any_pending = any(
            inv.status in ("pending", "adjudicating")
            for inv in session_history
        )
        if not any_pending:
            self._round_counter = (
                max(inv.round_no for inv in session_history) + 1
            )
        else:
            self._round_counter = max(
                inv.round_no for inv in session_history
            )
        return self._round_counter

    def _resolve_unit(self, notice: Notice) -> DrawUnit | None:
        units = {self.unit_by_member.get(m) for m in notice.members}
        units.discard(None)
        if len(units) != 1:
            return None
        return next(iter(units))

    def _check_waitlist_skip(
        self,
        notice: Notice,
        target: DrawUnit,
        headcount: int,
        seat_class: Pool,
    ) -> ReplayFinding | None:
        entries = self.waitlist(notice.session)
        target_rank = next(
            (e.rank for e in entries if e.unit.id == target.id), None
        )
        if target_rank is None:
            return None
        for entry in entries:
            if entry.rank >= target_rank:
                break
            if not entry.active:
                continue
            # 席类不匹配（优先单元的保护席仍在保留期、公开单元不能碰保护席等）
            # 或参与上限已满，都属于“不能直接利用该空位”，不构成违规跳过。
            if not self._unit_can_use(
                entry.unit, notice.session, seat_class, headcount
            ):
                continue
            return ReplayFinding(
                notice.at,
                Irregularity.WAITLIST_SKIP,
                notice.id,
                f"跳过仍有效且可利用空位的候补 {entry.unit.id}（rank {entry.rank}）"
                f"先发 {target.id}（rank {target_rank}）",
            )
        return None

    def _unit_can_use(
        self,
        unit: DrawUnit,
        session: str,
        seat_class: Pool,
        headcount: int,
    ) -> bool:
        if session not in unit.choices:
            return False
        # 严格按通知占用的席类判断该候补能否直接利用这笔空位：
        # 公开单元不能用保护席；优先单元的顺位由保护席承接，
        # 保护席仍在保留期时，公开席空位不构成它“可直接利用”的机会。
        if seat_class is Pool.PRIORITY and unit.pool is Pool.PUBLIC:
            return False
        if seat_class is Pool.PUBLIC and unit.pool is Pool.PRIORITY:
            return False
        # 已接受承诺达到个人跨场上限后，新的空位对该单元不再可利用。
        accepted = len(self._accepted_sessions(unit.members[0]))
        if not unit.together and accepted >= unit.per_person_limit:
            return False
        need = unit.headcount if unit.together else 1
        return self.seats[session][seat_class].free >= need

    def _has_priority_waitlist(self, session: str) -> bool:
        return any(
            entry.active and entry.unit.pool is Pool.PRIORITY
            for entry in self.waitlist(session)
        )

    def _deactivate_waitlist(self, unit: DrawUnit, session: str) -> None:
        for entry in self.waitlist(session):
            if entry.unit.id == unit.id:
                entry.active = False

    def _apply_receipt(self, notice: Notice) -> None:
        prior = self.invitations.get(notice.prior or "")
        if prior is None:
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.STALE_RECEIPT,
                    notice.id,
                    f"回执 {notice.id} 引用未知或已终结邀请 {notice.prior}",
                )
            )
            return
        # 裁决释放可以作用于已接受邀请（身份冲突裁决）；
        # 普通回执（接受/拒绝/到期）则要求邀请仍在待复状态。
        if notice.kind is not NoticeKind.ADJUDICATE_DROP and prior.status not in (
            "pending",
            "adjudicating",
        ):
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.STALE_RECEIPT,
                    notice.id,
                    f"邀请 {notice.prior} 已处于 {prior.status}，回执无效",
                )
            )
            return

        if notice.kind is NoticeKind.ACCEPTED:
            prior.status = "accepted"
            self._convert_hold_to_commit(prior)
            self._terminate_mutex_waitlist(prior)
        elif notice.kind in (NoticeKind.DECLINED, NoticeKind.EXPIRED):
            prior.status = notice.kind.value
            self._release_hold(prior)
        elif notice.kind is NoticeKind.ADJUDICATE_DROP:
            # 运营裁决释放：未裁决前占位仍有效；此处记录裁决结果并释放席位。
            prior.status = "adjudicate_drop"
            self._release_hold(prior)
            self.findings.append(
                ReplayFinding(
                    notice.at,
                    Irregularity.ADJUDICATION,
                    notice.id,
                    f"邀请 {notice.prior} 经运营裁决释放；该席位在裁决截至前一直占位、"
                    "不得提前当空位补发",
                )
            )

    def _convert_hold_to_commit(self, inv: InvitationState) -> None:
        state = self.seats[inv.notice.session][inv.seat_class]
        state.held -= inv.headcount
        state.committed += inv.headcount

    def _release_hold(self, inv: InvitationState) -> None:
        state = self.seats[inv.notice.session][inv.seat_class]
        state.held = max(0, state.held - inv.headcount)

    def _terminate_mutex_waitlist(self, inv: InvitationState) -> None:
        """接受可替代志愿后的候补处置。

        - 整组单元只可能落一场，接受后其他场的同单元候补互斥终止；
        - 个人单元按个人跨场上限判断：已接受场次数达到上限才终止
          其他场候补，仍有剩余容量时保留后续机会（不取消可并行参加）；
        - 同一家庭依规则可同时参加的其他成员，承诺不受影响。
        """
        unit = self.unit_by_id[inv.unit_id]
        for session, entries in self._waitlists.items():
            if session == inv.notice.session:
                continue
            for entry in entries:
                if entry.unit.id != unit.id:
                    continue
                if unit.together:
                    entry.active = False
                    entry.skip_reason = (
                        f"整组已接受 {inv.notice.session}，互斥候补终止"
                    )
                    continue
                accepted = len(self._accepted_sessions(unit.members[0]))
                if accepted >= unit.per_person_limit:
                    entry.active = False
                    entry.skip_reason = (
                        f"已接受 {accepted} 场达到上限 {unit.per_person_limit}，"
                        "互斥候补终止"
                    )
                # 未达上限：保留后续机会，不标记、不拒绝。

        # 同一家庭里依规则可同时参加的其他成员：只有参与上限会被突破时才停。
        own_household = self.scenario.household_of(unit.members[0])
        for other in self.units:
            if other.id == unit.id:
                continue
            other_household = self.scenario.household_of(other.members[0])
            if other_household != own_household:
                continue
            if self._still_under_limit(other, inv):
                continue  # 保留其候补
            for entries in self._waitlists.values():
                for entry in entries:
                    if entry.unit.id == other.id:
                        entry.active = False
                        entry.skip_reason = "参与上限将被突破"

    def _still_under_limit(self, other: DrawUnit, inv: InvitationState) -> bool:
        accepted = self._accepted_sessions(other.members[0])
        return len(accepted) < other.per_person_limit

    def _accepted_sessions(self, member: str) -> set[str]:
        result: set[str] = set()
        for inv in self.invitations.values():
            if inv.status != "accepted":
                continue
            unit = self.unit_by_id[inv.unit_id]
            if member in unit.members:
                result.add(inv.notice.session)
        return result

    # ------------------------------------------------------------------ #
    # 查询接口
    # ------------------------------------------------------------------ #
    def effective_capacity(self, session: str) -> dict[Pool, int]:
        """实际剩余容量：不含已发未回复席位（不拿保留席做分母）。"""
        return {
            pool: state.free
            for pool, state in self.seats[session].items()
        }

    def explain_hold(self, session: str, seat_class: Pool) -> HoldExplanation:
        """逐笔解释某类空位为何还不能补发。"""
        cap = self.data.capacities[session]
        state = self.seats[session][seat_class]
        blockers = [
            inv
            for inv in self.invitations.values()
            if inv.notice.session == session
            and inv.seat_class is seat_class
            and inv.status in ("pending", "adjudicating")
        ]
        if blockers:
            inv = blockers[0]
            reason = (
                HoldReason.PENDING_INVITATION
                if inv.status == "pending"
                else HoldReason.PENDING_INVITATION
            )
            return HoldExplanation(
                session,
                seat_class,
                state.free,
                reason,
                inv.notice.id,
                f"通知 {inv.notice.id} 已发未回复/裁决中，{inv.headcount} 席保留至 "
                f"{inv.notice.expires}",
            )
        if seat_class is Pool.PRIORITY and self.now < cap.release_at:
            return HoldExplanation(
                session,
                seat_class,
                state.free,
                HoldReason.PROTECTED_NOT_RELEASED,
                None,
                f"保护席 {cap.release_at} 才解除保留",
            )
        if seat_class is Pool.PRIORITY and self._has_priority_waitlist(session):
            return HoldExplanation(
                session,
                seat_class,
                state.free,
                HoldReason.PROTECTED_WAIT_PRIORITY,
                None,
                "已解保但仍有可递补优先单元，不得转公开",
            )
        active = [e for e in self.waitlist(session) if e.active]
        if not active:
            return HoldExplanation(
                session,
                seat_class,
                state.free,
                HoldReason.NO_ELIGIBLE_WAITLIST,
                None,
                "没有仍有效的候补单元",
            )
        return HoldExplanation(
            session,
            seat_class,
            state.free,
            HoldReason.FILLED,
            None,
            f"尚有 {len(active)} 个有效候补，可按顺位补发",
        )

    def convertible_protected(self, session: str) -> int:
        """解保时刻可转公开的保护席：实际空着且没有可递补优先单元。"""
        cap = self.data.capacities[session]
        if self.now < cap.release_at:
            return 0
        if self._has_priority_waitlist(session):
            return 0
        return self.seats[session][Pool.PRIORITY].free

    def next_sendable(self, session: str) -> tuple[WaitEntry, Pool] | None:
        """按原轮候补规则给出下一笔可直接发出的邀请。

        志愿或人数并非可直接利用的空位时暂跳过（保留后续机会），
        跳过不等于拒绝；没有可利用项时返回 None。
        """
        cap = self.data.capacities[session]
        released = self.now >= cap.release_at
        for entry in self.waitlist(session):
            if not entry.active or session not in entry.unit.choices:
                continue
            # 优先单元：先保护席后公开席。
            if entry.unit.pool is Pool.PRIORITY:
                if self.seats[session][Pool.PRIORITY].free >= (
                    entry.unit.headcount if entry.unit.together else 1
                ):
                    return entry, Pool.PRIORITY
                if self.seats[session][Pool.PUBLIC].free >= (
                    entry.unit.headcount if entry.unit.together else 1
                ):
                    return entry, Pool.PUBLIC
                entry.skip_reason = "整组空位不足，暂跳过保持后续机会"
                continue
            # 公开单元只能用公开席（未解保的保护席不可用）。
            need = entry.unit.headcount if entry.unit.together else 1
            public_free = self.seats[session][Pool.PUBLIC].free
            if released:
                public_free += self.convertible_protected(session)
            if public_free >= need:
                return entry, Pool.PUBLIC
            entry.skip_reason = "公开空位不足，暂跳过保持后续机会"
        return None
