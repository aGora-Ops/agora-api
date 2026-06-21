from fastapi import APIRouter

from app.api.v1.routes import analytics, auth, chat, orgs, remediations, runs, workflows

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(orgs.router, prefix="/orgs", tags=["organizations"])
api_router.include_router(workflows.router, prefix="/workflows", tags=["workflows"])
api_router.include_router(runs.router, prefix="/runs", tags=["runs"])
api_router.include_router(remediations.router, prefix="/remediations", tags=["remediations"])
api_router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
