"""公益活动室假期场次的抽签审计与递补引擎。

模块划分：

- :mod:`lottery.models`   领域对象（报名、场次、佐证、通知）
- :mod:`lottery.loader`   CSV 源记录装载与结构校验
- :mod:`lottery.identity` 身份/家庭佐证的可行解释枚举
- :mod:`lottery.units`    按一种身份解释构造抽签单元
- :mod:`lottery.draw`     公布机制下的抽签模拟、穷举与抽样
- :mod:`lottery.audit`    两类机会、机会差与证据不足范围审计
- :mod:`lottery.ledger`   通知台账回放、保留名额与按轮递补
- :mod:`lottery.engine`   两轮之间的身份核验事件与对外咨询口径
"""

from .models import (
    Application,
    Capacity,
    IdentityClaim,
    Notice,
    Pool,
    Mode,
    NoticeKind,
)
from .loader import load_tables, SourceData
from .identity import feasible_scenarios, Scenario, IdentityState
from .audit import audit_cutoff, AuditReport, GapVerdict
from .ledger import Ledger, HoldReason
from .engine import OperationsEngine

__all__ = [
    "Application",
    "Capacity",
    "IdentityClaim",
    "Notice",
    "Pool",
    "Mode",
    "NoticeKind",
    "load_tables",
    "SourceData",
    "feasible_scenarios",
    "Scenario",
    "IdentityState",
    "audit_cutoff",
    "AuditReport",
    "GapVerdict",
    "Ledger",
    "HoldReason",
    "OperationsEngine",
]
