"""FastAPI entry point: /health, the assistant's API (/ai) and the review screen (/review).

Run locally:  uvicorn app.api.main:app --reload
"""

from fastapi import FastAPI

from app.api.ai import router as ai_router
from app.api.review import router as review_router

app = FastAPI(title="Mechanic AI service", version="0.1.0")
app.include_router(ai_router)
app.include_router(review_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
