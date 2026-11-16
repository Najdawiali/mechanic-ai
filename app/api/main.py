"""FastAPI entry point: /health, the assistant's API (/ai), the review screen (/review) and a temporary demo page (/demo).

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


# DEMO: a temporary page for showing the service before the platform's apps call it (see app/demo/__init__.py).
# To remove the demo, delete this block and the app/demo folder.
from app.demo.router import router as demo_router

app.include_router(demo_router)
