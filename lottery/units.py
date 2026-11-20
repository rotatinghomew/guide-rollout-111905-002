"""按一种可行报名解释构造抽签单元。

单元是抽签排列中的最小实体：

- 可拆（separate）：每个实际参与者一个个人单元；同一人重报的多行合并，
  以最早报名行的志愿参加，跨场上限取适用的更严值（取最小）。
- 整组（together）：同一家庭号下须原子安排的成员构成一个整组单元，
  志愿取全员共同志愿、按最早报名顺序声明的志愿优先级；全员必须同场，
  送不进当前场可跳过，由后续场继续承接。
- 资格不全为优先类别的组只可用公开席。
"""

from __future__ import annotations

from dataclasses import dataclass

from .identity import Scenario
from .models import Application, Mode, Pool


@dataclass(frozen=True)
class DrawUnit:
    """一个参与抽签的单元。"""

    id: str                                  # 稳定标识，如 ``A3`` 或 ``A1+A2``
    members: tuple[str, ...]                 # 覆盖的报名行
    participants: frozenset[frozenset[str]]  # 覆盖的实际参与者（人组）
    choices: tuple[str, ...]                 # 声明的志愿顺序
    together: bool                           # 是否整组原子安排
    pool: Pool                               # 可用席类：全优先才可碰保护席
    per_person_limit: int                    # 共同适用的更严人次上限
    earliest_received: int

    @property
    def headcount(self) -> int:
        """占席人数（按报名行人数；同一人合并后只占一席）。"""
        return len(self.participants)


def competition_zones(
    applications: dict[str, Application],
) -> tuple[frozenset[str], ...]:
    """有志愿相交的场次同属一个竞争分区（并查集）。"""
    sessions: set[str] = set()
    for app in applications.values():
        sessions.update(app.choices)
    parent = {s: s for s in sessions}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    choice_sets = [set(app.choices) for app in applications.values()]
    ordered = sorted(sessions)
    for i, s1 in enumerate(ordered):
        for s2 in ordered[i + 1 :]:
            if any(s1 in cs and s2 in cs for cs in choice_sets):
                parent[find(s1)] = find(s2)

    zones: dict[str, set[str]] = {}
    for session in ordered:
        zones.setdefault(find(session), set()).add(session)
    return tuple(frozenset(v) for v in sorted(zones.values(), key=lambda z: sorted(z)))


def build_units(
    applications: dict[str, Application],
    scenario: Scenario,
) -> list[DrawUnit]:
    """把报名解释展开为抽签单元（全部竞争分区的并集）。"""
    # 1) 每个实际参与者选出代表行：最早报名的那行（重报以最早志愿参加）。
    representative: dict[frozenset[str], str] = {}
    person_limit: dict[frozenset[str], int] = {}
    for person_group in scenario.person_partition:
        rows = sorted(
            (applications[aid] for aid in person_group),
            key=lambda app: (app.received_at, app.id),
        )
        representative[person_group] = rows[0].id
        person_limit[person_group] = min(app.limit for app in rows)

    # 2) 按家庭号分组（只含截止有效报名），再按 mode 拆/合。
    household_rows: dict[frozenset[str], list[str]] = {}
    for hh_group in scenario.household_partition:
        household_rows[hh_group] = sorted(
            hh_group, key=lambda aid: (applications[aid].received_at, aid)
        )

    units: list[DrawUnit] = []
    for hh_group, row_ids in household_rows.items():
        together_rows = [
            aid for aid in row_ids if applications[aid].mode is Mode.TOGETHER
        ]
        separate_rows = [
            aid for aid in row_ids if applications[aid].mode is Mode.SEPARATE
        ]

        if together_rows:
            units.append(
                _build_group_unit(
                    together_rows, applications, scenario, representative, person_limit
                )
            )
        for aid in separate_rows:
            person_group = scenario.participant_of(aid)
            # 同一参与者只出一个个人单元（代表行所在家庭承载它）。
            if aid != representative[person_group]:
                continue
            units.append(
                _build_person_unit(
                    person_group, applications, representative, person_limit
                )
            )

    return sorted(units, key=lambda u: (u.earliest_received, u.id))


def _build_person_unit(
    person_group: frozenset[str],
    applications: dict[str, Application],
    representative: dict[frozenset[str], str],
    person_limit: dict[frozenset[str], int],
) -> DrawUnit:
    rep_id = representative[person_group]
    rep = applications[rep_id]
    all_priority = all(
        applications[aid].pool is Pool.PRIORITY for aid in person_group
    )
    return DrawUnit(
        id=rep_id,
        members=tuple(sorted(person_group)),
        participants=frozenset({person_group}),
        choices=rep.choices,
        together=False,
        pool=Pool.PRIORITY if all_priority else Pool.PUBLIC,
        per_person_limit=person_limit[person_group],
        earliest_received=min(applications[aid].received_at for aid in person_group),
    )


def _build_group_unit(
    row_ids: list[str],
    applications: dict[str, Application],
    scenario: Scenario,
    representative: dict[frozenset[str], str],
    person_limit: dict[frozenset[str], int],
) -> DrawUnit:
    rows = [applications[aid] for aid in row_ids]
    # 全员共同志愿；志愿优先级按最早报名行声明的顺序。
    common = set(rows[0].choices)
    for row in rows[1:]:
        common &= set(row.choices)
    order_row = min(rows, key=lambda app: (app.received_at, app.id))
    choices = tuple(s for s in order_row.choices if s in common)

    participant_groups = frozenset(
        scenario.participant_of(aid) for aid in row_ids
    )
    strictest_limit = min(
        person_limit[pg] for pg in participant_groups
    )
    all_priority = all(row.pool is Pool.PRIORITY for row in rows)
    return DrawUnit(
        id="+".join(sorted(row_ids)),
        members=tuple(sorted(row_ids)),
        participants=participant_groups,
        choices=choices,
        together=True,
        pool=Pool.PRIORITY if all_priority else Pool.PUBLIC,
        per_person_limit=strictest_limit,
        earliest_received=min(row.received_at for row in rows),
    )


def units_for_zone(units: list[DrawUnit], zone: frozenset[str]) -> list[DrawUnit]:
    """筛选志愿与该分区相交的单元（分区内排列只含这些单元）。"""
    return [u for u in units if set(u.choices) & zone]
