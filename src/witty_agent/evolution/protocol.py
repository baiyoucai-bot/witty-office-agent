"""评测工人必须回一段 YAML。"""

from __future__ import annotations

from dataclasses import dataclass

from witty_agent.logging import get_logger

logger = get_logger("evolution.protocol")


@dataclass
class EvalReport:
    status: str
    case_id: str = ""
    run: int = 1
    score: float | None = None
    session_id: str = ""
    reason: str = ""


def parse_eval_report(text: str) -> EvalReport:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.startswith("yaml"):
            body = body[4:]
        body = body.strip()
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    status = fields.get("status") or "invalid_request"
    score_raw = fields.get("score")
    score = None
    if score_raw not in {None, "", "null"}:
        try:
            score = float(score_raw)
        except ValueError:
            status = "invalid_request"
    report = EvalReport(
        status=status,
        case_id=fields.get("case_id") or "",
        run=int(fields.get("run") or 1),
        score=score,
        session_id=fields.get("session_id") or "",
        reason=fields.get("reason") or "",
    )
    if report.status == "ok" and report.score is None:
        report.status = "invalid_request"
    return report


def is_valid_matrix(reports: list[EvalReport], expected_version: int, actual_version: int) -> bool:
    if actual_version != expected_version:
        return False
    if not reports:
        return False
    return all(item.status == "ok" and item.score is not None for item in reports)
