"""报名截止口径的抽签审计。

从同一份可行报名解释出发，比较两类机会：

1. 个人机会：每个实际参与者获得任一可接受场次首轮邀请的概率，
   再按申请方式（整组 / 可拆）分别做按人平均。
2. 家庭全员同场机会：整组家庭所有成员在同一场拿到邀请的概率。

机会差与判定：

- ``gap_limit`` 是预先备案的、两种申请方式按人平均邀请机会的绝对差上限。
- 对每一种身份可行解释算出精确机会差；跨解释取并集得到“证据不足范围”。
- 范围整体不超限 -> 通过；整体超限 -> 不通过；跨线 -> 不能按通过发邀请，
  并列出可改变结论的佐证与可独立开放的场次。
- 抽样口径下，抽样误差与身份未定造成的差别分别陈述，合并区间跨线即不可通过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .draw import ExactResult, SampleResult, enumerate_zone, sample_zone
from .identity import Scenario, feasible_scenarios
from .loader import SourceData
from .units import DrawUnit, build_units, competition_zones


class GapVerdict(str, Enum):
    PASS = "pass"                 # 全部可行解释的机会差均不超过备案线
    FAIL = "fail"                 # 全部可行解释均超过备案线
    UNDECIDED = "undecided"       # 证据不足范围跨线，不得按通过发邀请


@dataclass
class ZoneAudit:
    zone: frozenset[str]
    gap_limit: float
    scenario_gaps: dict[str, float]                 # 场景指纹 -> 机会差
    gap_range: tuple[float, float]
    verdict: GapVerdict
    person_offer: dict[str, dict[str, float]]       # 场景 -> 参与者 -> 个人机会
    together_same_session: dict[str, dict[str, float]]
    mode_averages: dict[str, dict[str, float]]      # 场景 -> together/separate 均值
    deciding_claims: tuple[str, ...]                # 可改变结论的佐证
    independently_openable: bool                    # 该分区是否不依赖未定佐证
    exact: bool


@dataclass
class AuditReport:
    as_of: int
    scenarios: tuple[Scenario, ...]
    zones: tuple[ZoneAudit, ...]
    method: str = "exact"
    sampling_error: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def can_send_invitations(self) -> bool:
        """所有分区都通过才允许开第一封通知。"""
        return all(z.verdict is GapVerdict.PASS for z in self.zones)

    def openable_sessions(self) -> tuple[str, ...]:
        """判定不依赖未定佐证、可独立开放的场次。"""
        sessions: list[str] = []
        for zone in self.zones:
            if zone.verdict is GapVerdict.PASS and zone.independently_openable:
                sessions.extend(sorted(zone.zone))
        return tuple(sorted(sessions))

    def blocked_sessions(self) -> tuple[str, ...]:
        return tuple(
            sid
            for zone in self.zones
            if zone.verdict is not GapVerdict.PASS
            for sid in sorted(zone.zone)
        )


def _scenario_fingerprint(scenario: Scenario) -> str:
    return ";".join(
        f"{claim}={'T' if take else 'F'}"
        for claim, take in sorted(scenario.decisions)
    ) or "baseline"


def _participant_mode(units: list[DrawUnit]) -> dict[frozenset[str], str]:
    """参与者 -> 其单元的申请方式（整组/可拆）。"""
    result: dict[frozenset[str], str] = {}
    for unit in units:
        label = "together" if unit.together else "separate"
        for participant in unit.participants:
            result[participant] = label
    return result


def _zone_metrics(
    units: list[DrawUnit],
    exact: ExactResult,
) -> tuple[
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, list[float]],
]:
    """计算一个场景下一个分区的按人机会、家庭同场机会与分组均值。"""
    mode_of = _participant_mode(units)
    person_offer: dict[str, float] = {}
    grouped: dict[str, list[float]] = {"together": [], "separate": []}

    for unit in exact.units:
        p_offer = exact.offer_probability(unit.id)
        for participant in unit.participants:
            # 同一参与者组内每个报名行都能查到同一份机会
            # （身份合并后多行只对应一个实际参与者）。
            for application_id in participant:
                person_offer[application_id] = p_offer
            label = mode_of[participant]
            # 按“人”平均：一个参与者只计一次，不能按报名行重复计。
            grouped[label].append(p_offer)

    same_session: dict[str, float] = {}
    for unit in exact.units:
        if unit.together:
            same_session[unit.id] = (
                exact.placed_weight[unit.id] / exact.total_permutations
            )

    averages = {
        label: (sum(values) / len(values) if values else float("nan"))
        for label, values in grouped.items()
    }
    return person_offer, same_session, averages, grouped


def _gap_from_averages(averages: dict[str, float]) -> float:
    """两种申请方式按人平均机会的绝对差；缺任一方式时差值为 0。

    一个分区里若只有可拆（或只有整组）单元，该分区不存在两种方式之间
    的可说明差距，按 0 参与备案线判定。
    """
    if any(math.isnan(v) for v in averages.values()):
        return 0.0
    return abs(averages["together"] - averages["separate"])

def audit_cutoff(
    data: SourceData,
    *,
    sample_draws: int | None = None,
    seed: int = 20261120,
) -> AuditReport:
    """对截止时刻的资格与志愿做抽签审计。

    默认精确穷举；给 ``sample_draws`` 则改用固定种子抽样，并在报告中
    分别给出抽样误差区间与身份未定范围。
    """
    cutoff = data.cutoff
    scenarios = feasible_scenarios(data.applications, data.identity, cutoff)
    zones = competition_zones(data.applications)

    zone_audits: list[ZoneAudit] = []
    sampling_error: dict[str, tuple[float, float]] = {}

    for zone in zones:
        gap_limits = {
            data.capacities[sid].gap_limit
            for sid in zone
        }
        if len(gap_limits) != 1:
            raise ValueError(f"分区 {sorted(zone)} 内 gap_limit 不一致")
        gap_limit = next(iter(gap_limits))

        scenario_gaps: dict[str, float] = {}
        person_offer_all: dict[str, dict[str, float]] = {}
        same_session_all: dict[str, dict[str, float]] = {}
        averages_all: dict[str, dict[str, float]] = {}
        gap_sample_bounds: dict[str, tuple[float, float]] = {}

        for scenario in scenarios:
            fp = _scenario_fingerprint(scenario)
            units = build_units(data.eligible_applications(), scenario)

            if sample_draws is None:
                exact = enumerate_zone(units, data.capacities, zone)
                person_offer, same_session, averages, _ = _zone_metrics(
                    units, exact
                )
                gap = _gap_from_averages(averages)
            else:
                sample = sample_zone(
                    units, data.capacities, zone, sample_draws, seed
                )
                person_offer, same_session, averages, gap, bounds = (
                    _sampled_metrics(units, sample)
                )
                # 分区缺任一申请方式时不存在方式间差距，抽样误差也归零。
                if gap == 0.0:
                    bounds = (0.0, 0.0)
                gap_sample_bounds[fp] = bounds
            scenario_gaps[fp] = gap
            person_offer_all[fp] = person_offer
            same_session_all[fp] = same_session
            averages_all[fp] = averages

        lo = min(scenario_gaps.values())
        hi = max(scenario_gaps.values())
        if hi <= gap_limit + 1e-12:
            verdict = GapVerdict.PASS
        elif lo > gap_limit + 1e-12:
            verdict = GapVerdict.FAIL
        else:
            verdict = GapVerdict.UNDECIDED

        deciding = _deciding_claims(scenarios, scenario_gaps, gap_limit)
        independent = not deciding
        zone_audits.append(
            ZoneAudit(
                zone=zone,
                gap_limit=gap_limit,
                scenario_gaps=scenario_gaps,
                gap_range=(lo, hi),
                verdict=verdict,
                person_offer=person_offer_all,
                together_same_session=same_session_all,
                mode_averages=averages_all,
                deciding_claims=tuple(sorted(deciding)),
                independently_openable=independent,
                exact=sample_draws is None,
            )
        )
        if sample_draws is not None:
            # 抽样误差取各场景区间的外包，与身份范围分别保留。
            lows = [b[0] for b in gap_sample_bounds.values()]
            highs = [b[1] for b in gap_sample_bounds.values()]
            sampling_error[_zone_key(zone)] = (min(lows), max(highs))

    return AuditReport(
        as_of=cutoff,
        scenarios=tuple(scenarios),
        zones=tuple(zone_audits),
        method="exact" if sample_draws is None else f"sample(n={sample_draws})",
        sampling_error=sampling_error,
    )


def _zone_key(zone: frozenset[str]) -> str:
    return "+".join(sorted(zone))


def _sampled_metrics(
    units: list[DrawUnit],
    sample: SampleResult,
) -> tuple[
    dict[str, float],
    dict[str, float],
    dict[str, float],
    float,
    tuple[float, float],
]:
    """抽样口径：点估计与差值的近似 95% 区间（独立排列、独立同分布近似）。"""
    mode_of = _participant_mode(units)
    person_offer: dict[str, float] = {}
    grouped: dict[str, list[float]] = {"together": [], "separate": []}
    # 记录每个方式下的单元级命中率，用于差值标准误。
    hits: dict[str, list[int]] = {"together": [], "separate": []}

    for unit in sample.units:
        p_offer = sample.offer_probability(unit.id)
        hit = sample.offer_hits[unit.id]
        for participant in unit.participants:
            for application_id in participant:
                person_offer[application_id] = p_offer
            label = mode_of[participant]
            # 按参与者（人）计一次，而非按报名行重复计。
            grouped[label].append(p_offer)
            hits[label].append(hit)

    averages = {
        label: (sum(values) / len(values) if values else float("nan"))
        for label, values in grouped.items()
    }
    gap = _gap_from_averages(averages)

    # 差值标准误：两组独立单元在同一次排列上的命中差，按排列平均的方差。
    n = sample.draws
    se = _difference_standard_error(sample, units)
    sampling_lo = max(0.0, gap - 1.96 * se)
    sampling_hi = min(1.0, gap + 1.96 * se)

    same_session: dict[str, float] = {}
    for unit in sample.units:
        if unit.together:
            same_session[unit.id] = sample.placed_hits[unit.id] / n
    return person_offer, same_session, averages, gap, (sampling_lo, sampling_hi)


def _difference_standard_error(sample: SampleResult, units: list[DrawUnit]) -> float:
    """用单元分组的二项方差近似两种方式均值差的标准误。"""
    n = sample.draws
    components: list[float] = []
    for label in ("together", "separate"):
        group = [
            u for u in sample.units
            if (u.together if label == "together" else not u.together)
        ]
        if not group:
            continue
        m = len(group)
        phats = [sample.offer_hits[u.id] / n for u in group]
        var_mean = sum(p * (1 - p) for p in phats) / (m * m * n)
        components.append(var_mean)
    return math.sqrt(sum(components))


def _deciding_claims(
    scenarios: list[Scenario],
    scenario_gaps: dict[str, float],
    gap_limit: float,
) -> set[str]:
    """找出取值改变会让结论跨越备案线的佐证。"""
    fps = {s: _scenario_fingerprint(s) for s in scenarios}
    deciding: set[str] = set()
    claim_ids = {claim for s in scenarios for claim, _ in s.decisions}
    for claim in claim_ids:
        gaps_when_true = [
            scenario_gaps[fps[s]]
            for s in scenarios
            if dict(s.decisions).get(claim) is True
        ]
        gaps_when_false = [
            scenario_gaps[fps[s]]
            for s in scenarios
            if dict(s.decisions).get(claim) is False
        ]
        if not gaps_when_true or not gaps_when_false:
            continue
        sides = {
            g <= gap_limit + 1e-12
            for g in (*gaps_when_true, *gaps_when_false)
        }
        if len(sides) > 1:
            deciding.add(claim)
    return deciding
