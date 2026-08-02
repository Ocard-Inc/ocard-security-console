"""掃描結果的存檔與讀取。

存檔的理由：一份掃描報告是**產出當下的快照**。限制段落（IP 涵蓋率、探針有沒有跑）
描述的是那一刻的事實，事後重算會得到不同答案；而事件清單要能被引用、比對、
交接。所以整份 report 落盤，不是每次重跑。
"""
from __future__ import annotations

import json

from console.core import timewin
from console.store import db


def save(report: dict, *, created_by: str, duration_ms: int,
         include_api_probe: bool) -> str:
    """寫入一次掃描，回傳 sweep_no。"""
    summary = report["summary"]
    with db.tx() as conn:
        sweep_no = db.next_serial("SWEEP", "sweeps", "sweep_no")
        conn.execute(
            "INSERT INTO sweeps (sweep_no, range_start, range_end, include_api_probe,"
            " created_at, created_by, duration_ms, summary_json, limitations_json,"
            " probes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sweep_no, summary["range_start"], summary["range_end"],
             int(include_api_probe), timewin.fmt(timewin.taipei_now()), created_by,
             duration_ms,
             json.dumps(summary, ensure_ascii=False),
             json.dumps(report["limitations"], ensure_ascii=False),
             json.dumps(report["probes"], ensure_ascii=False)))
        conn.executemany(
            "INSERT INTO sweep_findings (sweep_no, rank, entity, entity_kind,"
            " risk_level, score, single_signal, finding_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(sweep_no, f["rank"], f["entity"], f["entity_kind"], f["risk_level"],
              f["score"], int(f["single_signal"]), json.dumps(f, ensure_ascii=False))
             for f in report["findings"]])
    return sweep_no


def load(sweep_no: str) -> dict | None:
    """讀回存檔的掃描報告，形狀與 report.build() 一致（多了 sweep_no 等欄位）。"""
    row = db.one(
        "SELECT sweep_no, range_start, range_end, include_api_probe, created_at,"
        " created_by, duration_ms, summary_json, limitations_json, probes_json,"
        " narrative_md, narrative_model, narrative_at"
        " FROM sweeps WHERE sweep_no = ?", (sweep_no,))
    if row is None:
        return None
    findings = db.rows(
        "SELECT finding_json FROM sweep_findings WHERE sweep_no = ? ORDER BY rank",
        (sweep_no,))
    return {
        "sweep_no": row["sweep_no"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "duration_ms": row["duration_ms"],
        "include_api_probe": bool(row["include_api_probe"]),
        "summary": json.loads(row["summary_json"]),
        "limitations": json.loads(row["limitations_json"]),
        "probes": json.loads(row["probes_json"]),
        "findings": [json.loads(r["finding_json"]) for r in findings],
        "narrative": {
            "markdown": row["narrative_md"],
            "model": row["narrative_model"],
            "generated_at": row["narrative_at"],
        } if row["narrative_md"] else None,
    }


def recent(limit: int = 20) -> list[dict]:
    rows = db.rows(
        "SELECT sweep_no, range_start, range_end, created_at, created_by,"
        " duration_ms, include_api_probe, summary_json,"
        " (narrative_md IS NOT NULL) AS has_narrative"
        " FROM sweeps ORDER BY id DESC LIMIT ?", (int(limit),))
    out = []
    for r in rows:
        summary = json.loads(r["summary_json"])
        out.append({
            "sweep_no": r["sweep_no"],
            "range_start": r["range_start"], "range_end": r["range_end"],
            "created_at": r["created_at"], "created_by": r["created_by"],
            "duration_ms": r["duration_ms"],
            "include_api_probe": bool(r["include_api_probe"]),
            "has_narrative": bool(r["has_narrative"]),
            "findings": summary.get("findings", 0),
            "by_level": summary.get("by_level", {}),
        })
    return out


def save_narrative(sweep_no: str, markdown: str, model: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE sweeps SET narrative_md = ?, narrative_model = ?, narrative_at = ?"
            " WHERE sweep_no = ?",
            (markdown, model, timewin.fmt(timewin.taipei_now()), sweep_no))
