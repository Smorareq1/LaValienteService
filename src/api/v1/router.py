from fastapi import APIRouter

from src.api.v1.endpoints import auth, authorization

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(authorization.router)
