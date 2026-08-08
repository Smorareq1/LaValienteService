from fastapi import APIRouter

from src.api.v1.endpoints import (
    auth,
    authorization,
    catalog,
    customers,
    daily_close,
    expenses,
    inventory,
    orders,
    promotions,
    scans,
    staff,
    supply_sales,
    sync,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(authorization.router)
api_router.include_router(catalog.router)
api_router.include_router(customers.router)
api_router.include_router(daily_close.router)
api_router.include_router(expenses.router)
api_router.include_router(inventory.router)
api_router.include_router(orders.router)
api_router.include_router(promotions.router)
api_router.include_router(scans.router)
api_router.include_router(staff.router)
api_router.include_router(supply_sales.router)
api_router.include_router(sync.router)
