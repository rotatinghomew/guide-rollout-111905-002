"""CSV 源记录装载与结构校验。

不同来源（抽签规则、席位变更、家庭报名、人工通知）分别读入，
在装载阶段只做结构性校验，不做业务推断——空值不声明事实、不等于零。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .models import (
    Application,
    Capacity,
    ClaimScope,
    ClaimState,
    IdentityClaim,
    Mode,
    Notice,
    NoticeKind,
    Pool,
    parse_clock,
)


class SourceError(ValueError):
    """源记录结构或引用不满足 DATA.md 约定。"""


@dataclass(frozen=True)
class OrderRow:
    """order.csv 一行：某轮某场人工留存的抽取顺位（一次实际顺位，不是概率）。"""

    id: str
    round: int
    session: str
    application: str
    rank: int


@dataclass(frozen=True)
class SourceData:
    applications: dict[str, Application]
    capacities: dict[str, Capacity]
    identity: list[IdentityClaim]
    order: list[OrderRow]
    notices: list[Notice]

    @property
    def cutoff(self) -> int:
        cutoffs = {c.cutoff for c in self.capacities.values()}
        if len(cutoffs) != 1:
            raise SourceError(f"各场报名截止时刻不一致: {sorted(cutoffs)}")
        return next(iter(cutoffs))

    def eligible_applications(self) -> dict[str, Application]:
        """截止时刻仍有效的报名（截止时资格与志愿构成抽取可用范围）。"""
        return {
            aid: app
            for aid, app in self.applications.items()
            if app.received_at <= self.cutoff
        }


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise SourceError(f"{path.name} 缺少字段名")
        rows = list(reader)
        if not rows:
            raise SourceError(f"{path.name} 没有数据行")
        for row in rows:
            if None in row or any(value is None for value in row.values()):
                raise SourceError(f"{path.name} 存在多余列或空字段名")
        return list(reader.fieldnames), rows


def _require_ids_unique(rows: list, field: str, table: str) -> None:
    ids = [getattr(row, field) for row in rows]
    if len(ids) != len(set(ids)):
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        raise SourceError(f"{table} 的 {field} 存在重复: {dupes}")


def load_tables(fixture_dir: str | Path) -> SourceData:
    """从合成 CSV 目录装载全部源记录并做引用完整性校验。"""
    fixture_dir = Path(fixture_dir)
    applications: dict[str, Application] = {}
    capacities: dict[str, Capacity] = {}
    identity: list[IdentityClaim] = []
    order: list[OrderRow] = []
    notices: list[Notice] = []

    _, app_rows = _read(fixture_dir / "applications.csv")
    for row in app_rows:
        applications[row["id"]] = Application(
            id=row["id"],
            household=row["household"],
            person=row["person"],
            choices=tuple(row["choices"].split(";")),
            pool=Pool(row["pool"]),
            mode=Mode(row["mode"]),
            limit=int(row["limit"]),
            received_at=parse_clock(row["received"]),
        )
    _require_ids_unique(list(applications.values()), "id", "applications")

    _, cap_rows = _read(fixture_dir / "capacity.csv")
    for row in cap_rows:
        capacities[row["id"]] = Capacity(
            id=row["id"],
            seats=int(row["seats"]),
            protected=int(row["protected"]),
            release_at=parse_clock(row["release_at"]),
            rule=row["rule"],
            cutoff=parse_clock(row["cutoff"]),
            gap_limit=float(row["gap_limit"]),
        )
    _require_ids_unique(list(capacities.values()), "id", "capacity")

    _, id_rows = _read(fixture_dir / "identity.csv")
    for row in id_rows:
        identity.append(
            IdentityClaim(
                id=row["id"],
                left=row["left"],
                right=row["right"],
                scope=ClaimScope(row["scope"]),
                state=ClaimState(row["state"]),
                received_at=parse_clock(row["received"]),
                supersedes=row["supersedes"] or None,
            )
        )
    _require_ids_unique(identity, "id", "identity")

    _, order_rows = _read(fixture_dir / "order.csv")
    for row in order_rows:
        order.append(
            OrderRow(
                id=row["id"],
                round=int(row["round"]),
                session=row["session"],
                application=row["application"],
                rank=int(row["rank"]),
            )
        )
    _require_ids_unique(order, "id", "order")

    _, notice_rows = _read(fixture_dir / "responses.csv")
    for row in notice_rows:
        notices.append(
            Notice(
                id=row["id"],
                prior=row["prior"] or None,
                session=row["session"],
                members=tuple(row["members"].split(";")),
                hold_class=Pool(row["hold_class"]),
                kind=NoticeKind(row["kind"]),
                at=parse_clock(row["at"]),
                expires=parse_clock(row["expires"]) if row["expires"] else None,
            )
        )
    _require_ids_unique(notices, "id", "responses")

    data = SourceData(
        applications=applications,
        capacities=capacities,
        identity=identity,
        order=order,
        notices=notices,
    )
    _validate_references(data)
    return data


def _validate_references(data: SourceData) -> None:
    app_ids = set(data.applications)
    cap_ids = set(data.capacities)
    id_ids = {c.id for c in data.identity}

    for app in data.applications.values():
        unknown = [s for s in app.choices if s not in cap_ids]
        if unknown:
            raise SourceError(f"报名 {app.id} 引用了不存在的场次: {unknown}")
        if app.limit < 1:
            raise SourceError(f"报名 {app.id} 的跨场上限必须为正")

    for claim in data.identity:
        for endpoint in (claim.left, claim.right):
            if endpoint not in app_ids:
                raise SourceError(f"佐证 {claim.id} 引用了不存在的报名 {endpoint}")
        if claim.supersedes and claim.supersedes not in id_ids:
            raise SourceError(
                f"佐证 {claim.id} 的 supersedes 指向不存在的佐证 {claim.supersedes}"
            )

    for row in data.order:
        if row.application not in app_ids:
            raise SourceError(f"顺位 {row.id} 引用了不存在的报名 {row.application}")
        if row.session not in cap_ids:
            raise SourceError(f"顺位 {row.id} 引用了不存在的场次 {row.session}")

    notice_ids = {n.id for n in data.notices}
    for notice in data.notices:
        if notice.prior and notice.prior not in notice_ids:
            raise SourceError(
                f"通知 {notice.id} 的 prior 指向不存在的通知 {notice.prior}"
            )
        if notice.session not in cap_ids:
            raise SourceError(f"通知 {notice.id} 引用了不存在的场次 {notice.session}")
        for member in notice.members:
            if member not in app_ids:
                raise SourceError(f"通知 {notice.id} 引用了不存在的报名 {member}")
