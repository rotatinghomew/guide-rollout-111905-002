"""领域对象：全部源记录的不可变表示。

时间统一折算为当天本地时钟的“分钟数”，左闭右开语义不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Pool(str, Enum):
    """资格层：优先资格可用保护席与公开席；公开资格只能用公开席。"""

    PRIORITY = "priority"
    PUBLIC = "public"


class Mode(str, Enum):
    """家庭报名方式。

    ``together`` 整组原子安排（全员必须同场，送不进可被跳过）；
    ``separate`` 可拆，视为个人单元。
    """

    TOGETHER = "together"
    SEPARATE = "separate"


class NoticeKind(str, Enum):
    """通知/回执类型。"""

    SENT = "sent"                       # 发出邀请（产生通知与保留）
    ACCEPTED = "accepted"               # 接受（承诺成立，席位确定占用）
    DECLINED = "declined"               # 拒绝（释放该邀请的保留）
    EXPIRED = "expired"                 # 到期未复（与拒绝同效，记录口径区分）
    ADJUDICATE_DROP = "adjudicate_drop"  # 运营裁决释放；未裁决前占位仍有效


def parse_clock(value: str) -> int:
    """``HH:MM`` -> 从零点起的分钟数。"""
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


@dataclass(frozen=True)
class Application:
    """一行报名档案（报名承诺）。

    ``household`` 是报名时填写的家庭号；未经佐证核实的两个家庭号
    不得假定已合并，也不得当作两组独立机会分别发号。
    """

    id: str
    household: str
    person: str
    choices: tuple[str, ...]            # 志愿顺序，从先到后
    pool: Pool
    mode: Mode
    limit: int                          # 个人跨场上限（人次可接受场数）
    received_at: int

    @property
    def is_together(self) -> bool:
        return self.mode is Mode.TOGETHER


@dataclass(frozen=True)
class Capacity:
    """一场的席位与保留名额规则。"""

    id: str
    seats: int                          # 总名额
    protected: int                      # 其中保护席数量
    release_at: int                     # 保护席解除保留时刻
    rule: str                           # 规则批次标识（如 R1）
    cutoff: int                         # 报名截止时刻
    gap_limit: float                    # 预先备案的两种申请方式机会差上限

    @property
    def public_seats(self) -> int:
        return self.seats - self.protected


class ClaimState(str, Enum):
    """佐证状态。空值不声明事实。"""

    POSSIBLE = "possible"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"


class ClaimScope(str, Enum):
    """佐证指向：同一实际参与者，或同一家庭号。"""

    PERSON = "person"
    HOUSEHOLD = "household"


@dataclass(frozen=True)
class IdentityClaim:
    """一条身份/同住关系佐证。

    ``supersedes`` 指向被本条取代的旧佐证；被取代的佐证不再生效。
    同一候选人不能同时对应两个不同的人（由装载校验与场景枚举共同保证）。
    """

    id: str
    left: str                           # applications.id
    right: str                          # applications.id
    scope: ClaimScope
    state: ClaimState
    received_at: int
    supersedes: Optional[str] = None


@dataclass(frozen=True)
class Notice:
    """一封通知或一条回执（responses.csv 的一行）。

    ``prior`` 为空表示新发出的邀请；非空表示对该邀请的回执。
    ``members`` 是本通知涉及的报名行 ID（整组邀请含多行）。
    保护席邀请 ``hold_class=priority``，公开席邀请为 ``public``。
    """

    id: str
    prior: Optional[str]
    session: str
    members: tuple[str, ...]
    hold_class: Pool
    kind: NoticeKind
    at: int
    expires: Optional[int] = None

    @property
    def is_invitation(self) -> bool:
        return self.kind is NoticeKind.SENT
