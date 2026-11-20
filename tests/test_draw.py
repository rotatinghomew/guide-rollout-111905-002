"""抽签机制：席位处理、精确穷举、抽样与顺位一致性测试。"""
import _paths  # noqa: F401  确保仓库根在 sys.path

import unittest

from lottery.draw import (
    SeatLedger,
    brute_force_zone,
    check_order_consistency,
    enumerate_zone,
    run_permutation,
    sample_zone,
)
from lottery.identity import feasible_scenarios
from lottery.loader import load_tables
from lottery.models import Pool
from lottery.units import build_units, competition_zones

FIXTURE = "/workspace/fixtures"


class DrawMechanism(unittest.TestCase):
    def setUp(self):
        self.data = load_tables(FIXTURE)
        self.scenario = feasible_scenarios(
            self.data.applications, self.data.identity, self.data.cutoff
        )[0]
        self.units = build_units(
            self.data.eligible_applications(), self.scenario
        )
        self.z1 = frozenset({"S1", "S2", "S3"})
        self.z4 = frozenset({"S4"})

    def test_competition_zones(self):
        zones = competition_zones(self.data.applications)
        self.assertEqual(set(zones), {self.z1, self.z4})

    def test_dp_matches_brute_force_main_zone(self):
        exact = enumerate_zone(self.units, self.data.capacities, self.z1)
        brute = brute_force_zone(self.units, self.data.capacities, self.z1)
        self.assertEqual(
            exact.total_permutations, brute.total_permutations
        )
        self.assertEqual(exact.offer_weight, brute.offer_weight)
        self.assertEqual(exact.placed_weight, brute.placed_weight)

    def test_dp_matches_brute_force_s4(self):
        exact = enumerate_zone(self.units, self.data.capacities, self.z4)
        brute = brute_force_zone(self.units, self.data.capacities, self.z4)
        self.assertEqual(exact.offer_weight, brute.offer_weight)
        self.assertEqual(exact.total_permutations, 2)

    def test_s4_everyone_always_gets_an_offer(self):
        exact = enumerate_zone(self.units, self.data.capacities, self.z4)
        for unit_id, weight in exact.offer_weight.items():
            self.assertEqual(weight, exact.total_permutations)

    def test_group_unit_is_atomic_and_never_split(self):
        # 任意排列下，整组单元要么全员同场、要么没有席位。
        brute = brute_force_zone(self.units, self.data.capacities, self.z1)
        from itertools import permutations

        zone_units = [u for u in self.units if set(u.choices) & self.z1]
        for ordered in permutations(zone_units):
            results = run_permutation(
                list(ordered), self.data.capacities, self.z1
            )
            for unit in zone_units:
                if not unit.together:
                    continue
                placement = results[unit.id]
                if placement.sessions:
                    self.assertEqual(len(placement.sessions), 1)
                    total_seats = sum(
                        p + q for p, q in placement.seat_use.values()
                    )
                    self.assertEqual(total_seats, unit.headcount)

    def test_public_unit_cannot_touch_protected_seats(self):
        zone_units = [u for u in self.units if set(u.choices) & self.z1]
        from itertools import permutations

        for ordered in permutations(zone_units):
            results = run_permutation(
                list(ordered), self.data.capacities, self.z1
            )
            for unit in zone_units:
                if unit.pool is not Pool.PUBLIC:
                    continue
                for prot, pub in results[unit.id].seat_use.values():
                    self.assertEqual(prot, 0)

    def test_priority_uses_protected_before_public(self):
        # 构造：A1+A2 优先整组排第一时，应吃掉 S1 两个保护席。
        ordered = [u for u in self.units if u.id == "A1+A2"] + [
            u for u in self.units if u.id != "A1+A2"
        ]
        results = run_permutation(ordered, self.data.capacities, self.z1)
        prot, pub = results["A1+A2"].seat_use["S1"]
        self.assertEqual((prot, pub), (2, 0))

    def test_order_file_contains_cross_session_cycle(self):
        consistency = check_order_consistency(
            self.data.order, self.units, self.z1
        )
        self.assertFalse(consistency.consistent)
        self.assertTrue(consistency.cycle)

    def test_sampling_reproduces_exact_within_error(self):
        sample = sample_zone(
            self.units, self.data.capacities, self.z1, 8000, seed=7
        )
        exact = enumerate_zone(self.units, self.data.capacities, self.z1)
        for unit_id in exact.offer_weight:
            p_exact = exact.offer_probability(unit_id)
            low, high = sample.wilson_interval(unit_id)
            self.assertLessEqual(low, p_exact + 0.03)
            self.assertGreaterEqual(high, p_exact - 0.03)

    def test_seat_ledger_rollback_on_insufficient_capacity(self):
        ledger = SeatLedger.initial(self.data.capacities, self.z1)
        before = ledger.snapshot()
        self.assertIsNone(ledger.take("S3", Pool.PUBLIC, 99))
        self.assertEqual(ledger.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
