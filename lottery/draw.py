"""公布抽签机制的执行与机会计算。

机制（DATA.md capacity/order 口径）：

1. 首轮在相同资格层及相交志愿的竞争分区内，对有效报名单元做等概率
   随机全排列；所有可能顺位同等概率，已发生的单一顺位不是概率。
2. 按排列顺序、依单元公布志愿依次处理，填到个人限额为止；
   整组单元需要全员同场、整组送不进当前场可跳过，由后续志愿承接。
3. 优先单元先用保护席、再用公开席；公开单元只能用公开席；
   资格不全为优先的整组按公开单元处理。
4. 场次席位不能跨场搬用；首轮保护席未解除，只在本场本席类内占用。

机会计算提供三种口径：

- :func:`enumerate_zone`  动态规划穷举全部排列，返回精确概率（小规模）
- :func:`brute_force_zone` 直接枚举排列，仅用于与 DP 结果交叉验证
- :func:`sample_zone`      固定种子抽样，附 Wilson/标准误区间
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import cache

from .loader import OrderRow
from .models import Capacity, Pool
from .units import DrawUnit


@dataclass(frozen=True)
class Placement:
    """一个单元在一场排列下获得的邀请。"""

    unit_id: str
    sessions: tuple[str, ...]
    # 每场实际占用的席类明细：session -> (保护席数, 公开席数)
    seat_use: dict[str, tuple[int, int]]


@dataclass
class SeatLedger:
    """一场排列处理过程中的剩余席位（按场按席类）。"""

    remaining: dict[str, list[int]]  # session -> [保护席剩余, 公开席剩余]

    @classmethod
    def initial(cls, capacities: dict[str, Capacity], zone: frozenset[str]):
        return cls(
            remaining={
                sid: [cap.protected, cap.public_seats]
                for sid, cap in sorted(capacities.items())
                if sid in zone
            }
        )

    def snapshot(self) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            (sid, prot, pub) for sid, (prot, pub) in sorted(self.remaining.items())
        )

    @classmethod
    def from_snapshot(cls, snapshot):
        return cls(
            remaining={sid: [prot, pub] for sid, prot, pub in snapshot}
        )

    def take(self, session: str, pool: Pool, headcount: int) -> tuple[int, int] | None:
        """尝试在本场占用 headcount 个席位，返回席类明细；不足则不动账。"""
        prot_rem, pub_rem = self.remaining[session]
        if pool is Pool.PRIORITY:
            use_prot = min(headcount, prot_rem)
            use_pub = headcount - use_prot
            if use_pub > pub_rem:
                return None
        else:
            use_prot = 0
            use_pub = headcount
            if use_pub > pub_rem:
                return None
        self.remaining[session][0] -= use_prot
        self.remaining[session][1] -= use_pub
        return use_prot, use_pub


def place_unit(
    unit: DrawUnit,
    ledger: SeatLedger,
    accepted_counts: dict[str, int],
) -> Placement:
    """按公布志愿处理一个单元，返回它拿到的邀请（可能为空）。"""
    seat_use: dict[str, tuple[int, int]] = {}
    sessions: list[str] = []

    if unit.together:
        # 整组原子安排：逐志愿找第一场能整组容纳的场次。
        for session in unit.choices:
            if session not in ledger.remaining:
                continue
            detail = ledger.take(session, unit.pool, unit.headcount)
            if detail is not None:
                seat_use[session] = detail
                sessions.append(session)
                for participant in unit.participants:
                    accepted_counts[next(iter(participant))] = (
                        accepted_counts.get(next(iter(participant)), 0) + 1
                    )
                break
        return Placement(unit.id, tuple(sessions), seat_use)

    # 个人单元：逐志愿、逐场填到个人限额。
    for session in unit.choices:
        if session not in ledger.remaining:
            continue
        if accepted_counts.get(unit.id, 0) >= unit.per_person_limit:
            break
        detail = ledger.take(session, unit.pool, 1)
        if detail is not None:
            seat_use[session] = detail
            sessions.append(session)
            accepted_counts[unit.id] = accepted_counts.get(unit.id, 0) + 1
    return Placement(unit.id, tuple(sessions), seat_use)


def run_permutation(
    ordered_units: list[DrawUnit],
    capacities: dict[str, Capacity],
    zone: frozenset[str],
) -> dict[str, Placement]:
    """执行一条具体排列，返回每个单元的安置结果。"""
    ledger = SeatLedger.initial(capacities, zone)
    counts: dict[str, int] = {}
    results: dict[str, Placement] = {}
    for unit in ordered_units:
        results[unit.id] = place_unit(unit, ledger, counts)
    return results


# --------------------------------------------------------------------------- #
# 精确穷举：对“剩余单元集合 × 席位状态 × 个人已接受场数”做记忆化递归。
# 每次递归任选一个剩余单元作为排列下一位，所有排列各计一次、等概率。
# --------------------------------------------------------------------------- #


@dataclass
class ExactResult:
    total_permutations: int
    offer_weight: dict[str, int]       # 单元拿到 ≥1 邀请的排列数
    placed_weight: dict[str, int]      # 整组单元成功同场安置的排列数
    units: list[DrawUnit]

    def offer_probability(self, unit_id: str) -> float:
        return self.offer_weight[unit_id] / self.total_permutations


def enumerate_zone(
    units: list[DrawUnit],
    capacities: dict[str, Capacity],
    zone: frozenset[str],
) -> ExactResult:
    """DP 穷举分区内全部等概率排列的精确机会。"""
    zone_units = [u for u in units if set(u.choices) & zone]
    n = len(zone_units)
    full_mask = (1 << n) - 1
    sessions = tuple(sorted(zone))
    initial_seats = tuple(
        (sid, capacities[sid].protected, capacities[sid].public_seats)
        for sid in sessions
    )
    initial_counts = tuple(0 for _ in zone_units)

    @cache
    def solve(mask: int, seats: tuple, counts: tuple):
        if mask == 0:
            return 1, tuple(0 for _ in zone_units), tuple(0 for _ in zone_units)
        total = 0
        offer = [0] * n
        placed = [0] * n
        for idx in range(n):
            bit = 1 << idx
            if mask & bit == 0:
                continue
            unit = zone_units[idx]
            ledger = SeatLedger.from_snapshot(seats)
            count_map = {
                u.id: counts[j]
                for j, u in enumerate(zone_units)
                if not u.together
            }
            placement = place_unit(unit, ledger, count_map)
            new_seats = ledger.snapshot()
            new_counts = list(counts)
            if not unit.together:
                new_counts[idx] = count_map[unit.id]
            sub_total, sub_offer, sub_placed = solve(
                mask ^ bit, new_seats, tuple(new_counts)
            )
            total += sub_total
            for j in range(n):
                offer[j] += sub_offer[j]
                placed[j] += sub_placed[j]
            if placement.sessions:
                offer[idx] += sub_total
                if unit.together:
                    placed[idx] += sub_total
        return total, tuple(offer), tuple(placed)

    total, offer, placed = solve(full_mask, initial_seats, initial_counts)
    return ExactResult(
        total_permutations=total,
        offer_weight={u.id: w for u, w in zip(zone_units, offer)},
        placed_weight={u.id: w for u, w in zip(zone_units, placed)},
        units=zone_units,
    )


def brute_force_zone(
    units: list[DrawUnit],
    capacities: dict[str, Capacity],
    zone: frozenset[str],
) -> ExactResult:
    """直接枚举全部排列（仅小规模校验用）。"""
    from itertools import permutations

    zone_units = [u for u in units if set(u.choices) & zone]
    offer = {u.id: 0 for u in zone_units}
    placed = {u.id: 0 for u in zone_units}
    total = 0
    for ordered in permutations(zone_units):
        results = run_permutation(list(ordered), capacities, zone)
        total += 1
        for unit in zone_units:
            if results[unit.id].sessions:
                offer[unit.id] += 1
                if unit.together:
                    placed[unit.id] += 1
    return ExactResult(total, offer, placed, zone_units)


# --------------------------------------------------------------------------- #
# 抽样口径：抽样误差与身份未定误差必须分别陈述。
# --------------------------------------------------------------------------- #


@dataclass
class SampleResult:
    draws: int
    offer_hits: dict[str, int]
    placed_hits: dict[str, int]
    units: list[DrawUnit]
    seed: int

    def offer_probability(self, unit_id: str) -> float:
        return self.offer_hits[unit_id] / self.draws

    def wilson_interval(self, unit_id: str, confidence: float = 0.95) -> tuple[float, float]:
        """单单元邀请概率的 Wilson 置信区间。"""
        z = {0.95: 1.96, 0.99: 2.576}[confidence]
        n = self.draws
        phat = self.offer_hits[unit_id] / n
        denom = 1 + z * z / n
        center = (phat + z * z / (2 * n)) / denom
        half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
        return max(0.0, center - half), min(1.0, center + half)


def sample_zone(
    units: list[DrawUnit],
    capacities: dict[str, Capacity],
    zone: frozenset[str],
    draws: int,
    seed: int,
) -> SampleResult:
    """固定种子做等概率排列抽样。"""
    zone_units = [u for u in units if set(u.choices) & zone]
    rng = random.Random(seed)
    offer = {u.id: 0 for u in zone_units}
    placed = {u.id: 0 for u in zone_units}
    for _ in range(draws):
        ordered = zone_units.copy()
        rng.shuffle(ordered)
        results = run_permutation(ordered, capacities, zone)
        for unit in zone_units:
            if results[unit.id].sessions:
                offer[unit.id] += 1
                if unit.together:
                    placed[unit.id] += 1
    return SampleResult(draws, offer, placed, zone_units, seed)


# --------------------------------------------------------------------------- #
# order.csv 是人工留存的“一次实际顺位”，按场分别记录。
# 机制要求一个竞争分区只有一条排列，因此跨场的前后关系必须无环、相容。
# --------------------------------------------------------------------------- #


@dataclass
class OrderConsistency:
    consistent: bool
    cycle: tuple[tuple[str, str], ...]          # 构成矛盾的前后关系
    duplicated_ranks: tuple[tuple[str, int], ...]
    missing_units: tuple[str, ...]


def check_order_consistency(
    order_rows: list[OrderRow],
    units: list[DrawUnit],
    zone: frozenset[str],
    round_no: int = 1,
) -> OrderConsistency:
    """检查分区内各场留存顺位能否由同一条排列解释。"""
    unit_ids = {u.id for u in units if set(u.choices) & zone}
    # 报名行 -> 单元（order.csv 记的是报名行；整组任一行代表该单元）。
    row_to_unit: dict[str, str] = {}
    for unit in units:
        for member in unit.members:
            row_to_unit[member] = unit.id

    edges: set[tuple[str, str]] = set()
    seen_ranks: dict[tuple[str, int], str] = {}
    dupes: list[tuple[str, int]] = []
    referenced: set[str] = set()

    for row in order_rows:
        if row.round != round_no or row.session not in zone:
            continue
        unit_id = row_to_unit.get(row.application, row.application)
        key = (row.session, row.rank)
        if key in seen_ranks:
            dupes.append(key)
        seen_ranks[key] = unit_id
        referenced.add(unit_id)

    # 同场内按 rank 生成前后关系。
    per_session: dict[str, list[tuple[int, str]]] = {}
    for row in order_rows:
        if row.round != round_no or row.session not in zone:
            continue
        unit_id = row_to_unit.get(row.application, row.application)
        per_session.setdefault(row.session, []).append((row.rank, unit_id))
    for entries in per_session.values():
        entries.sort()
        for (_, a), (_, b) in zip(entries, entries[1:]):
            if a != b:
                edges.add((a, b))

    # 有向图找环（DFS）。
    graph: dict[str, set[str]] = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set())
    color: dict[str, int] = {node: 0 for node in graph}
    stack: list[str] = []
    cycle: list[tuple[str, str]] = []

    def dfs(node: str) -> bool:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt, 0) == 1:
                start = stack.index(nxt)
                cycle_chain = stack[start:] + [nxt]
                cycle.extend(
                    (cycle_chain[i], cycle_chain[i + 1])
                    for i in range(len(cycle_chain) - 1)
                )
                return True
            if color.get(nxt, 0) == 0 and dfs(nxt):
                return True
        stack.pop()
        color[node] = 2
        return False

    bad = False
    for node in sorted(graph):
        if color[node] == 0 and dfs(node):
            bad = True
            break

    missing = tuple(sorted(unit_ids - referenced))
    return OrderConsistency(
        consistent=not bad and not dupes,
        cycle=tuple(cycle),
        duplicated_ranks=tuple(sorted(set(dupes))),
        missing_units=missing,
    )
