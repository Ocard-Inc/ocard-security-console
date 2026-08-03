"""把探針命中以「對象」為軸合併，並計算命中了幾個彼此獨立的訊號。

純函式模組（不查 ClickHouse、不查 SQLite），因此可以用手工組的 Hit 直接測試。

## 交叉命中兩項以上才算事件

這是報告第三節的判讀方法，也是這整個功能的判準：單一門檻無法判定「異常登入」，
但四個彼此獨立的訊號同時亮起就很難是巧合。

## 為什麼計票單位是 signal_group 而不是探針

「量級突變」「敏感路由總量」「路由集中於單一端點」三支會對同一個爆量帳號同時亮，
因為它們衡量的是同一件事的不同切面。用探針數計票的話，一個帳號輕易拿到 3 票、
分數虛高，排序就失去意義 —— 真正需要被排到前面的是「量級 + 來源型態 + 憑證集中」
這種**不同性質**的訊號同時成立的對象。所以同一組內多支命中只算一票，
並取該組內最強的一支作為代表。
"""
from __future__ import annotations

from dataclasses import dataclass

from console.sweep.probes import SUFFICIENT_ALONE
from console.sweep.run import Hit

# 進入事件清單所需的獨立訊號組數。報告的判讀方法是「交叉命中兩項以上」。
MIN_SIGNAL_GROUPS = 2


@dataclass(frozen=True)
class Candidate:
    """一個對象（帳號或來源）在這次掃描中的全部命中。"""
    entity_fp: str
    entity_kind: str                 # actor / src
    hits: tuple[Hit, ...]

    @property
    def signal_groups(self) -> tuple[str, ...]:
        """命中的相異訊號組，依首次出現順序（探針表順序）去重。"""
        seen: list[str] = []
        for h in self.hits:
            if h.signal_group not in seen:
                seen.append(h.signal_group)
        return tuple(seen)

    @property
    def probe_ids(self) -> tuple[str, ...]:
        return tuple(h.probe_id for h in self.hits)

    def strongest_in(self, signal_group: str) -> Hit | None:
        """該組內最強的命中（以 metric / floor 的倍率為準，不是原始 metric ——
        不同探針的 metric 單位不同，直接比大小沒有意義）。"""
        in_group = [h for h in self.hits if h.signal_group == signal_group]
        if not in_group:
            return None
        return max(in_group, key=lambda h: h.metric / max(h.floor, 1))


def is_suppressed(hit: Hit, suppressed_srcs: frozenset[str] | set[str]) -> bool:
    """這筆命中是否被 allowlist 抑制。

    **必須檢查 `entity_kind`。** allowlist 只收來源 IP；不看 kind 的話，一筆
    字面上剛好等於某個帳號名的條目會把**那個帳號**整筆從報告裡抹掉 ——
    而這個檔案自己的 docstring 就寫著「帳號與來源是不同的對象…不該合在一起」。
    """
    return hit.entity_kind == "src" and hit.entity_fp in suppressed_srcs


def correlate(
    hits: tuple[Hit, ...] | list[Hit],
    *,
    suppressed_srcs: frozenset[str] | set[str] = frozenset(),
) -> tuple[Candidate, ...]:
    """合併命中為候選對象清單（尚未評分、尚未套 MIN_SIGNAL_GROUPS 門檻）。

    合併鍵是 `(entity_kind, entity_fp)`：帳號與來源是不同的對象，即使 fingerprint
    理論上不會相撞也不該合在一起 —— 報告的事件清單同樣是帳號與 IP 並列、各自成列。

    `suppressed_srcs` 是**全域** allowlist 內的來源 IP（如辦公室出口），
    不列入候選。實測辦公室出口 `1.34.41.218` 在三天內就用了 68 個帳號，
    不抑制的話它永遠是第一名。被抑制的命中由 report.build() 另外收集並在報告裡
    列出來（含「若不抑制會是第幾名」）—— 只給一個數字的話沒有人判斷得出
    這條例外還該不該存在。

    kwarg 從 `suppressed_fps` 改名為 `suppressed_srcs` 是刻意的：語意由
    「任何 fingerprint」收窄成「來源 IP」，舊呼叫端必須 TypeError 而不是
    靜靜地用舊語意通過。
    """
    buckets: dict[tuple[str, str], list[Hit]] = {}
    for h in hits:
        if is_suppressed(h, suppressed_srcs):
            continue
        buckets.setdefault((h.entity_kind, h.entity_fp), []).append(h)

    return tuple(
        Candidate(entity_fp=fp, entity_kind=kind, hits=tuple(group))
        for (kind, fp), group in buckets.items()
    )


def qualifies_alone(candidate: Candidate) -> str | None:
    """單一訊號豁免：回傳讓它成立的 signal_group，沒有就回 None。

    見 probes.SUFFICIENT_ALONE 的說明 —— 偽造來源標頭與大規模憑證集中這兩件事
    沒有無害的解釋，要求它們再湊一個訊號才肯呈報，等於把最強的證據壓在清單外。
    實測那筆偽造 `X-Forwarded-For: 127.0.0.1`（報告事件 1 的前置探測）全期只有
    128 列、只命中這一支探針，靠 MIN_SIGNAL_GROUPS 永遠上不了清單。
    """
    for hit in candidate.hits:
        required = SUFFICIENT_ALONE.get(hit.signal_group)
        if required is not None and hit.metric >= required * max(hit.floor, 1e-9):
            return hit.signal_group
    return None


def split_by_threshold(
    candidates: tuple[Candidate, ...],
) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
    """回傳 (達門檻的, 未達門檻的)。

    達門檻 = 交叉命中 MIN_SIGNAL_GROUPS 個獨立訊號組，**或**單一訊號足以成立
    （見 qualifies_alone）。

    未達門檻的刻意也回傳而不是丟掉：報告要能誠實說出「另有 N 個對象只命中單一訊號、
    未列入清單」。靜靜濾掉會讓讀者以為清單就是全部。
    """
    def ok(c: Candidate) -> bool:
        return len(c.signal_groups) >= MIN_SIGNAL_GROUPS or qualifies_alone(c) is not None

    strong = tuple(c for c in candidates if ok(c))
    weak = tuple(c for c in candidates if not ok(c))
    return strong, weak
