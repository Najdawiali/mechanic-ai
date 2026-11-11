"""FastAPI entry point. Only /health for now; the contract in handoff 7.3 is agreed in days 6-8."""

from fastapi import FastAPI

app = FastAPI(title="Mechanic AI service", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
