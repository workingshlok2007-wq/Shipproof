from fastapi import FastAPI

from app.links import auditLink
from app.deadlines import reconcileDeadlines

app = FastAPI()


@app.post("/audit")
def runAudit(urls: list[str]) -> list[dict]:
    return [auditLink(url) for url in urls]


@app.post("/deadlines/reconcile")
def reconcile(deadlines: list[dict]) -> dict:
    return reconcileDeadlines(deadlines)
