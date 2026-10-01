from fastapi import APIRouter
from app.api.v1.stocks import router as stocks_router
from app.api.v1.prices import router as prices_router
from app.api.v1.events import router as events_router
from app.api.v1.companies import router as companies_router

api_router = APIRouter()

api_router.include_router(stocks_router)
api_router.include_router(prices_router)
api_router.include_router(events_router)
api_router.include_router(companies_router)

__all__ = ["api_router"]
