"""風險評分與分級。純函式模組。

分數 = Σ（每個命中訊號組的權重 × 該組最強命中的規模係數）

兩個設計選擇值得說明：

**權重來自報告第三節對訊號強度的判讀**，不是均分。「來源型態為機房」的判別力
遠高於「非上班時間」——真人不會從資料中心登入後台，但半夜查訂單只是加班。
權重表在 probes.SIGNAL_WEIGHTS。

**規模係數用 metric / floor 的對數，不用原始 metric。** 不同探針的 metric 單位
完全不同（P04 的 metric 是帳號數、P01 是單日請求數），直接比大小沒有意義；
除以各自的地板才可比。取對數是因為量級差異可以到 4 個數量級
（實測 andrew_c 單日 772,870 對地板 500），線性計分會讓單一對象的分數
壓過所有其他訊號的組合。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from console.sweep.correlate import Candidate
from console.sweep.probes import SIGNAL_WEIGHTS

# 規模係數上下限。1.0 = 剛好踩到地板；4.0 = 地板的 1000 倍以上。
_MIN_SCALE = 1.0
_MAX_SCALE = 4.0

# 分級門檻。以報告的五級（極高／高／中高／中／中低）對齊，
# 用實測資料校準過：andrew_c（volume + concentration + off_hours）約 18 分落在極高，
# 只命中兩組中低權重訊號的對象落在中低。
_LEVELS = (
    (15.0, "極高"),
    (10.0, "高"),
    (6.0, "中高"),
    (3.5, "中"),
    (0.0, "中低"),
)


@dataclass(frozen=True)
class GroupContribution:
    signal_group: str
    weight: float
    scale: float
    probe_id: str
    probe_name: str
    points: float


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    level: str
    contributions: tuple[GroupContribution, ...]


def scale_factor(metric: float, floor: float) -> float:
    """規模係數：1 + log10(metric / floor)，夾在 [1, 4]。"""
    ratio = metric / max(floor, 1e-9)
    if ratio <= 1:
        return _MIN_SCALE
    return min(_MAX_SCALE, _MIN_SCALE + math.log10(ratio))


def level_for(score: float) -> str:
    for threshold, label in _LEVELS:
        if score >= threshold:
            return label
    return _LEVELS[-1][1]


def score_candidate(candidate: Candidate) -> Scored:
    contributions: list[GroupContribution] = []
    for group in candidate.signal_groups:
        hit = candidate.strongest_in(group)
        if hit is None:                       # signal_groups 由 hits 導出，理論上不會發生
            continue
        weight = SIGNAL_WEIGHTS.get(group, 1.0)
        scale = scale_factor(hit.metric, hit.floor)
        contributions.append(GroupContribution(
            signal_group=group, weight=weight, scale=round(scale, 3),
            probe_id=hit.probe_id, probe_name=hit.probe_name,
            points=round(weight * scale, 3),
        ))
    total = round(sum(c.points for c in contributions), 2)
    return Scored(candidate=candidate, score=total, level=level_for(total),
                  contributions=tuple(contributions))


def rank(candidates: tuple[Candidate, ...] | list[Candidate]) -> tuple[Scored, ...]:
    """評分並依風險排序。同分時訊號組多的在前（交叉命中越多越難是巧合）。"""
    scored = [score_candidate(c) for c in candidates]
    scored.sort(key=lambda s: (s.score, len(s.contributions)), reverse=True)
    return tuple(scored)
