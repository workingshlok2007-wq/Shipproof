from datetime import datetime


def reconcileDeadlines(deadlines: list[dict]) -> dict:
    """Newest dated rule wins; undated conflicts are reported, never guessed."""
    dated = [d for d in deadlines if d.get("issuedAt")]
    if len(dated) != len(deadlines):
        return {"resolved": False, "reason": "undated conflict"}
    newest = max(dated, key=lambda d: datetime.fromisoformat(d["issuedAt"]))
    return {"resolved": True, "deadline": newest["deadlineAt"]}
