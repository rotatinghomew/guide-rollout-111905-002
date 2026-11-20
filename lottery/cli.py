"""命令行审计入口。

用法：

    python3 -m lottery.cli audit    [--fixtures DIR] [--sample N --seed K]
    python3 -m lottery.cli replay   [--fixtures DIR]
    python3 -m lottery.cli consult [--fixtures DIR] --household H6

只输出聚合口径与本家庭数据，不暴露其他家庭身份。
"""

from __future__ import annotations

import argparse
import json

from .audit import audit_cutoff
from .engine import OperationsEngine
from .loader import load_tables


def _cmd_audit(args: argparse.Namespace) -> int:
    data = load_tables(args.fixtures)
    kwargs = {}
    if args.sample:
        kwargs["sample_draws"] = args.sample
        kwargs["seed"] = args.seed
    report = audit_cutoff(data, **kwargs)
    payload = {
        "method": report.method,
        "as_of_minutes": report.as_of,
        "scenario_count": len(report.scenarios),
        "can_send_invitations": report.can_send_invitations,
        "openable_sessions": list(report.openable_sessions()),
        "blocked_sessions": list(report.blocked_sessions()),
        "zones": [
            {
                "sessions": sorted(zone.zone),
                "verdict": zone.verdict.value,
                "gap_limit": zone.gap_limit,
                "identity_uncertainty_gap_range": [
                    zone.gap_range[0],
                    zone.gap_range[1],
                ],
                "deciding_claims": list(zone.deciding_claims),
                "independently_openable": zone.independently_openable,
                "mode_average_offer": {
                    fp: {k: v for k, v in av.items()}
                    for fp, av in zone.mode_averages.items()
                },
                "family_same_session_offer": {
                    fp: av for fp, av in zone.together_same_session.items()
                },
            }
            for zone in report.zones
        ],
        "sampling_error": report.sampling_error,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if report.can_send_invitations else 2


def _cmd_replay(args: argparse.Namespace) -> int:
    data = load_tables(args.fixtures)
    engine = OperationsEngine(data)
    engine.run_cutoff_audit()
    ledger = engine.open_ledger()
    payload = {
        "findings": [
            {
                "at_minutes": f.at,
                "kind": f.kind.value,
                "notice": f.notice_id,
                "detail": f.detail,
            }
            for f in ledger.findings
        ],
        "effective_capacity": {
            sid: {pool.value: n for pool, n in ledger.effective_capacity(sid).items()}
            for sid in sorted(data.capacities)
        },
        "next_sendable": {
            sid: _sendable(ledger, sid) for sid in sorted(data.capacities)
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _sendable(ledger, session: str):
    result = ledger.next_sendable(session)
    if result is None:
        return None
    entry, seat_class = result
    return {"unit": entry.unit.id, "seat_class": seat_class.value}


def _cmd_consult(args: argparse.Namespace) -> int:
    data = load_tables(args.fixtures)
    engine = OperationsEngine(data)
    engine.run_cutoff_audit()
    engine.open_ledger()
    view = engine.household_consultation(args.household)
    print(
        json.dumps(
            {
                "household": view.household,
                "cutoff_odds": view.cutoff_explanation,
                "commitments": list(view.commitments),
                "next_positions": list(view.next_positions),
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )
    return 0


def _json_default(value):
    if isinstance(value, frozenset):
        return sorted(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"不可序列化: {type(value)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lottery", description="抽签审计与递补")
    parser.add_argument(
        "--fixtures", default="fixtures", help="CSV 源记录目录"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="截止口径机会审计")
    audit.add_argument("--sample", type=int, default=None, help="抽样次数")
    audit.add_argument("--seed", type=int, default=20261120)
    audit.set_defaults(func=_cmd_audit)

    replay = sub.add_parser("replay", help="通知台账回放与递补检查")
    replay.set_defaults(func=_cmd_replay)

    consult = sub.add_parser("consult", help="单个家庭脱敏咨询")
    consult.add_argument("--household", required=True)
    consult.set_defaults(func=_cmd_consult)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
