# ShipProof Demo App

A small service that audits hackathon submission links and reconciles conflicting deadlines before a team submits.

Watch the [demo video]({{BASE}}/video-public.html) or browse the [old slides]({{BASE}}/slides-old.html).

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app
```

The service exposes an audit endpoint and a deadline endpoint. Results are returned as JSON so a dashboard can render them.
