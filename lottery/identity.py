"""身份与同住关系佐证的可行解释枚举。

口径（DATA.md）：

- ``possible`` 是“可成立但未核实”，既不能假定已合并，也不能当作两组
  独立机会直接发号；审计把每一种可行解释都算一遍，得到证据不足范围。
- ``confirmed`` / ``refuted`` 是已定性的事实；被 ``supersedes`` 取代
  的旧佐证不再生效；晚于查询时刻到达的佐证不算当时已知。
- 同一候选人不能同时对应两个不同的人：任一解释中，被 ``refuted`` 的
  两端必须落在不同组，且每个报名行只能属于一个参与者组。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .models import Application, ClaimScope, ClaimState, IdentityClaim


@dataclass(frozen=True)
class IdentityState:
    """某一时刻仍生效的佐证，按事实与待证两类拆开。"""

    as_of: int
    confirmed_person: tuple[frozenset[str], ...]
    refuted_person: tuple[tuple[str, str], ...]
    possible_person: tuple[IdentityClaim, ...]
    confirmed_household: tuple[frozenset[str], ...]
    refuted_household: tuple[tuple[str, str], ...]
    possible_household: tuple[IdentityClaim, ...]
    inactive_claims: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    """一种可行的报名解释。

    ``person_partition``：同一实际参与者的报名行分组；
    ``household_partition``：同一家庭号的报名行分组（人仍可不同）；
    ``decisions``：每条待证佐证在本解释下取真还是取假。
    """

    person_partition: tuple[frozenset[str], ...]
    household_partition: tuple[frozenset[str], ...]
    decisions: frozenset[tuple[str, bool]]

    def participant_of(self, application_id: str) -> frozenset[str]:
        for group in self.person_partition:
            if application_id in group:
                return group
        raise KeyError(application_id)

    def household_of(self, application_id: str) -> frozenset[str]:
        for group in self.household_partition:
            if application_id in group:
                return group
        raise KeyError(application_id)


def active_claims(claims: list[IdentityClaim], as_of: int) -> list[IdentityClaim]:
    """返回 ``as_of`` 时刻仍生效的佐证（已到达、且未被同样已到达的佐证取代）。

    取代关系只在取代者本身到达后才生效：截止时刻一条 ``possible``
    佐证即使日后会被确认/反驳取代，在当时仍是“可成立但未核实”的事实。
    """
    arrived = {c.id for c in claims if c.received_at <= as_of}
    superseded = {
        c.supersedes
        for c in claims
        if c.supersedes and c.id in arrived
    }
    return [
        claim
        for claim in claims
        if claim.id in arrived and claim.id not in superseded
    ]


def build_state(claims: list[IdentityClaim], as_of: int) -> IdentityState:
    """把生效佐证整理成事实边与待证边。"""
    alive = active_claims(claims, as_of)
    inactive = tuple(
        claim.id
        for claim in claims
        if claim not in alive
    )

    def closure(edges: list[tuple[str, str]]) -> tuple[frozenset[str], ...]:
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        for a, b in edges:
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            union(a, b)
        groups: dict[str, set[str]] = {}
        for node in list(parent):
            groups.setdefault(find(node), set()).add(node)
        return tuple(frozenset(g) for g in groups.values())

    confirmed_p_edges: list[tuple[str, str]] = []
    refuted_p: list[tuple[str, str]] = []
    possible_p: list[IdentityClaim] = []
    confirmed_h_edges: list[tuple[str, str]] = []
    refuted_h: list[tuple[str, str]] = []
    possible_h: list[IdentityClaim] = []

    for claim in alive:
        edge = (claim.left, claim.right)
        if claim.scope is ClaimScope.PERSON:
            if claim.state is ClaimState.CONFIRMED:
                confirmed_p_edges.append(edge)
            elif claim.state is ClaimState.REFUTED:
                refuted_p.append(edge)
            else:
                possible_p.append(claim)
        else:
            if claim.state is ClaimState.CONFIRMED:
                confirmed_h_edges.append(edge)
            elif claim.state is ClaimState.REFUTED:
                refuted_h.append(edge)
            else:
                possible_h.append(claim)

    return IdentityState(
        as_of=as_of,
        confirmed_person=closure(confirmed_p_edges),
        refuted_person=tuple(sorted(refuted_p)),
        possible_person=tuple(possible_p),
        confirmed_household=closure(confirmed_h_edges),
        refuted_household=tuple(sorted(refuted_h)),
        possible_household=tuple(possible_h),
        inactive_claims=inactive,
    )


def _extend_partition(
    base: tuple[frozenset[str], ...],
    universe: list[str],
    extra_edges: list[tuple[str, str]],
    refuted: tuple[tuple[str, str], ...],
) -> tuple[frozenset[str], ...] | None:
    """在事实分组上叠加待证边，返回闭包分组；若与反驳边冲突则返回 None。"""
    parent = {node: node for node in universe}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for group in base:
        members = list(group)
        for other in members[1:]:
            union(members[0], other)
    for a, b in extra_edges:
        union(a, b)

    groups: dict[str, set[str]] = {}
    for node in universe:
        groups.setdefault(find(node), set()).add(node)
    partition = tuple(frozenset(g) for g in groups.values())

    def together(a: str, b: str) -> bool:
        return find(a) == find(b)

    for a, b in refuted:
        if together(a, b):
            return None
    return partition


def feasible_scenarios(
    applications: dict[str, Application],
    claims: list[IdentityClaim],
    as_of: int,
) -> list[Scenario]:
    """枚举 ``as_of`` 时刻与全部已知事实相容的报名解释。

    每条待证佐证独立取真/取假（2^n），取真者按传递闭包合并；
    合并后若把已反驳的两端连到一起，或让一个报名行同时归入两个
    不同参与者，则该解释不可行、剔除。
    """
    state = build_state(claims, as_of)
    universe = sorted(applications)

    p_claims = list(state.possible_person)
    h_claims = list(state.possible_household)

    # 家庭号的基础分组先按报名档案给出，再叠加家庭佐证。
    base_household: dict[str, set[str]] = {}
    for aid, app in applications.items():
        base_household.setdefault(app.household, set()).add(aid)
    household_base = tuple(frozenset(v) for v in base_household.values())
    # 已确认的家庭合并边并入基础分组。
    household_base = _merge_base(household_base, state.confirmed_household)

    scenarios: list[Scenario] = []
    for p_flags in product((False, True), repeat=len(p_claims)):
        p_edges = [
            (claim.left, claim.right)
            for claim, take in zip(p_claims, p_flags)
            if take
        ]
        person_partition = _extend_partition(
            state.confirmed_person, universe, p_edges, state.refuted_person
        )
        if person_partition is None:
            continue
        # 同一候选人不能对应两个不同人：分组本身是划分，天然满足；
        # 再显式校验每个报名行恰好出现一次。
        seen: set[str] = set()
        for group in person_partition:
            if seen & group:
                person_partition = None  # pragma: no cover - 划分构造不会触发
                break
            seen |= group
        if person_partition is None or seen != set(universe):
            continue

        for h_flags in product((False, True), repeat=len(h_claims)):
            h_edges = [
                (claim.left, claim.right)
                for claim, take in zip(h_claims, h_flags)
                if take
            ]
            household_partition = _extend_partition(
                household_base, universe, h_edges, state.refuted_household
            )
            if household_partition is None:
                continue
            decisions = frozenset(
                (claim.id, take)
                for claim, take in (
                    *(zip(p_claims, p_flags)),
                    *(zip(h_claims, h_flags)),
                )
            )
            scenarios.append(
                Scenario(
                    person_partition=tuple(
                        sorted(person_partition, key=lambda g: sorted(g))
                    ),
                    household_partition=tuple(
                        sorted(household_partition, key=lambda g: sorted(g))
                    ),
                    decisions=decisions,
                )
            )
    return scenarios


def _merge_base(
    base: tuple[frozenset[str], ...],
    extra: tuple[frozenset[str], ...],
) -> tuple[frozenset[str], ...]:
    """把已确认的合并分组并入按家庭号得到的基础分组。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for group in (*base, *extra):
        members = list(group)
        for other in members[1:]:
            union(members[0], other)
    groups: dict[str, set[str]] = {}
    for node in list(parent):
        groups.setdefault(find(node), set()).add(node)
    return tuple(frozenset(g) for g in groups.values())
