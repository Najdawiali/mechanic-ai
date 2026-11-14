"""FastAPI entry point: /health and the review screen. The assistant's own endpoints come in Milestone 3.

Run locally:  uvicorn app.api.main:app --reload
"""

from fastapi import FastAPI

from app.api.review import router as review_router

app = FastAPI(title="Mechanic AI service", version="0.1.0")
app.include_router(review_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
