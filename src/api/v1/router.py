from fastapi import APIRouter

from src.api.v1.endpoints import auth, authorization, catalog, customers, orders, sync

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(authorization.router)
api_router.include_router(catalog.router)
api_router.include_router(customers.router)
api_router.include_router(orders.router)
api_router.include_router(sync.router)
