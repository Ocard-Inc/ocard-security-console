"""LLM 研判草稿。

## 送出去的是什麼

只有 `report.build()` 的結構化輸出：fingerprint、數字、探針名稱、訊號標籤、
限制段落。**沒有原始 IP、帳號、token、訂單號、headers 或 params 原文** ——
遮罩在探針層就完成了，這一層拿不到原文，不是「記得不要送」而是「沒有可送的」。

## 為什麼不讓 LLM 寫 SQL

分桶對齊、時間邊界、基線粒度配對這些約束一旦錯了不會報錯，而是靜靜給出錯的
數字（見 CLAUDE.md 的硬性約束）。查詢全部寫死在 probes.py，LLM 只負責把已經
算好的數字組織成人看得懂的研判。

## 失敗一律降級，不擋畫面

沒有 API key、被安全分類器拒絕、API 掛掉 —— 全部回 `markdown=None` 加一句
`error`，讓前端照樣顯示確定性的結果並標明「AI 研判不可用」。
LLM 從來不是這個功能的必要條件。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from console.core.config import settings

logger = logging.getLogger(__name__)

# 前端必須顯示這句。草稿寫進 events.judgement_note 前一定要人工按確認。
DISCLAIMER = "本段由 AI 依掃描結果的統計摘要產生，屬草稿，需人工確認後才可作為判定依據。"

SYSTEM_PROMPT = """\
你是 Ocard 資安監控主控台的分析助手。使用者剛跑完一次「期間異常掃描」，
你會收到那份掃描的結構化結果（JSON）。

請以繁體中文寫出一份研判草稿，格式為 Markdown，結構如下：

## 摘要
兩到四句。這段期間最重大的發現是什麼、涵蓋多少對象。先講結論。

## 事件研判
針對風險最高的前幾個對象，每個一小節。每節依序寫：
- **現象**：命中了哪些訊號、關鍵數字（引用實際 metric 與倍數）
- **異常點**：為什麼這個組合可疑，或為什麼可能是良性的
- **研判**：你的判斷，並明確標示信心水準（已證實／高度可疑／待確認）
- **建議動作**：具體、可執行的下一步

## 需要注意的限制
把 limitations 裡真正會影響上面結論的挑出來說明，不要照抄全部。

硬性要求：
- 對象一律以 fingerprint 稱呼（如 `actor_56CAB2602AA1`）。你拿不到原始帳號或 IP，
  不要猜測、不要編造人名、公司名或地理位置。
- 只使用 JSON 裡實際出現的數字。不確定就說不確定，不要補足或推估。
- `single_signal: true` 的對象**沒有經過交叉驗證**，研判必須明說這一點。
- 區分「沒有找到異常」與「這段期間查不到」。limitations 裡 level 為 blocking 的
  項目代表某類判讀根本不成立，不可寫成「未發現異常」。
- 不要建議任何會洩漏原始識別值的動作。
- 語氣是給資安人員看的工作文件，不是行銷文案。不要用表情符號。
"""


@dataclass(frozen=True)
class Narrative:
    markdown: str | None
    model: str
    error: str | None = None


def _config() -> dict:
    return settings().get("llm") or {}


def enabled() -> bool:
    return bool(_config().get("enabled"))


def model_name() -> str:
    return str(_config().get("model") or "claude-opus-5")


def _payload(stored: dict) -> str:
    """送給模型的內容。刻意只挑會用到的欄位，減少 token 也減少誤讀空間。"""
    return json.dumps({
        "summary": stored["summary"],
        "findings": stored["findings"],
        "limitations": stored["limitations"],
    }, ensure_ascii=False)


def write(stored: dict) -> Narrative:
    """產生研判草稿。任何失敗都回 markdown=None，不拋例外。"""
    model = model_name()
    if not enabled():
        return Narrative(None, model, "settings.yaml 的 llm.enabled 為 false，未啟用 AI 研判。")
    if not stored.get("findings"):
        return Narrative(None, model, "這次掃描沒有達門檻的對象，無需研判。")

    try:
        import anthropic
    except ImportError:
        return Narrative(None, model, "未安裝 anthropic 套件（uv add anthropic）。")

    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 - 缺 API key 等設定問題
        return Narrative(None, model, f"無法建立 Anthropic client：{exc}")

    try:
        # 走 streaming：thinking 在 Claude Opus 5 預設開啟，而 max_tokens 同時蓋住
        # thinking 與回應文字，非串流的大 max_tokens 會撞上 HTTP timeout。
        with client.messages.stream(
            model=model,
            max_tokens=int(_config().get("max_tokens") or 16000),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _payload(stored)}],
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - 逐一分類對呼叫端沒有意義，一律降級
        logger.warning("AI 研判呼叫失敗：%s", exc)
        return Narrative(None, model, f"呼叫 Claude API 失敗：{exc}")

    # 安全分類器可能拒絕請求：HTTP 200 但 content 為空或只有部分內容。
    # 不檢查就會在 content[0] 拋 IndexError。本工作是防禦性資安分析，正常應通過，
    # 但誤判會發生，而且拒絕不是錯誤 —— 要當成「這次沒有草稿」處理。
    if message.stop_reason == "refusal":
        detail = getattr(message, "stop_details", None)
        category = getattr(detail, "category", None) or "未分類"
        logger.info("AI 研判被安全分類器拒絕（category=%s）", category)
        return Narrative(
            None, model,
            f"Claude 的安全分類器拒絕了這次請求（類別：{category}）。"
            "確定性的掃描結果不受影響；如反覆發生請回報，勿以原始資料重試。")

    text = "\n".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    if not text.strip():
        return Narrative(None, model, "模型沒有回傳文字內容。")
    return Narrative(text.strip(), getattr(message, "model", model))
