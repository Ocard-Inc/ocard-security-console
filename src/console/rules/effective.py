"""生效中的規則 = YAML + SQLite 的參數覆寫。

`loader.load_rules()` 是「檔案裡寫了什麼」，這裡是「現在實際在跑什麼」。
五分鐘檢查（checker/tick.py）與回測（checker/replay.py）一律用這個模組。

**刻意不加 lru_cache。** 每個 tick 多一趟 SQLite（一張只有幾列的表）換到的是
「從 UI 改完，下一個 tick 就生效」。加了快取的話覆寫要重啟 server 才生效，
而且**不會有任何錯誤訊息** —— 使用者只會看到自己改的值沒有作用。

**也不要用 `load_rules.cache_clear()` 來求「立刻生效」。** 那是最容易被想到
的做法，也是錯的：cache_clear 重讀的是 YAML 不是覆寫，對覆寫毫無幫助，
卻會讓 16 個 YAML 的解析與 SQL 驗證發生在任意一條請求執行緒裡。

`Rule` 是 `frozen=True`，所以覆寫走 `dataclasses.replace()` 產生新物件 ——
沒有人能就地改掉 lru_cache 裡那份共用的 YAML 版本。

**這個做法順帶解決 store/events.py 的 cooldown。** `Finding.rule` 就是
engine 收到的那個實例，所以只要餵給 `evaluate()` 的 tuple 是覆寫後的，
`f.rule.cooldown_minutes` 自動是生效值 —— events.py 一行都不用改。
若改成「在每個使用點各自查覆寫」，那條路徑會是漏掉的那一個，
症狀是「cooldown 改了但通知節奏沒變」。
"""
from __future__ import annotations

from dataclasses import replace

from console.rules.loader import load_rules
from console.rules.model import Rule
from console.store import rule_overrides

# 覆寫欄位屬於 Rule 本身還是嵌套的 Threshold
_RULE_LEVEL = {"enabled": bool, "cooldown_minutes": int, "min_events": float}
_THRESHOLD_LEVEL = {"static_floor": float, "factor": float}


def editable_fields(rule: Rule) -> tuple[str, ...]:
    """這條規則實際上改得動哪些欄位。

    逐 kind 決定，不是一份通用清單：
    - `sql_threshold` 有 Threshold → 四個欄位都有效。
    - `new_source`（R08A/B/C）**沒有 Threshold**，門檻是 `min_events`。
      把覆寫寫進 threshold.static_floor 的話「存了、API 回新值、引擎用舊值」。
    - `freshness`（R12）完全忽略 rule.threshold，門檻讀
      `settings().freshness.alert_minutes` → 只有 enabled 與 cooldown 有意義。

    factor 另外要求有 baseline_key：沒有基線時 `_resolve_threshold` 的動態部分
    恆為 0，改 factor 不報錯也不生效。
    """
    if rule.kind == "sql_threshold" and rule.threshold is not None:
        fields = ["enabled", "static_floor", "cooldown_minutes"]
        if rule.threshold.baseline_key:
            fields.append("factor")
        return tuple(fields)
    if rule.kind == "new_source":
        return ("enabled", "cooldown_minutes", "min_events")
    return ("enabled", "cooldown_minutes")


def yaml_values(rule: Rule) -> dict:
    """這條規則在 YAML 裡的原值（只含它改得動的欄位）。"""
    out: dict[str, object] = {}
    for f in editable_fields(rule):
        if f in _THRESHOLD_LEVEL:
            out[f] = getattr(rule.threshold, f)
        else:
            out[f] = getattr(rule, f)
    return out


def apply_to(rule: Rule, override: dict) -> Rule:
    """套用覆寫，回傳新的 Rule（原物件不動）。不適用的欄位一律忽略。"""
    allowed = set(editable_fields(rule))
    rule_kwargs: dict[str, object] = {}
    threshold_kwargs: dict[str, object] = {}
    for field, cast in (*_RULE_LEVEL.items(), *_THRESHOLD_LEVEL.items()):
        if field not in allowed or field not in override:
            continue
        value = override[field]
        if value is None:
            continue
        target = threshold_kwargs if field in _THRESHOLD_LEVEL else rule_kwargs
        target[field] = cast(value)
    if threshold_kwargs and rule.threshold is not None:
        rule_kwargs["threshold"] = replace(rule.threshold, **threshold_kwargs)
    return replace(rule, **rule_kwargs) if rule_kwargs else rule


def effective_rules() -> tuple[Rule, ...]:
    """目前實際生效的規則。順序與 load_rules() 相同。"""
    overrides = rule_overrides.all_overrides()
    if not overrides:
        return load_rules()
    return tuple(apply_to(r, overrides.get(r.id, {})) for r in load_rules())


def prune(rule_id: str, values: dict) -> dict:
    """把「值等於 YAML 原值」的欄位改成 None（= 清掉覆寫）。

    存冗餘覆寫的兩個壞處：畫面上的「已覆寫」標記變成謊話；日後有人改了
    YAML，這條規則會被凍結在舊值上而畫面顯示「未覆寫」。
    """
    rule = next((r for r in load_rules() if r.id == rule_id), None)
    if rule is None:
        return values
    base = yaml_values(rule)
    return {f: (None if f in base and v == base[f] else v) for f, v in values.items()}
