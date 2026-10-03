from fastapi import APIRouter
from app.api.v1.stocks import router as stocks_router
from app.api.v1.prices import router as prices_router
from app.api.v1.events import router as events_router
from app.api.v1.companies import router as companies_router
from app.api.v1.market_data import router as market_data_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.signals import router as signals_router
from app.api.v1.screeners import router as screener_router
from app.api.v1.backtests import router as backtests_router
from app.api.v1.portfolios import router as portfolios_router

api_router = APIRouter()

api_router.include_router(stocks_router)
api_router.include_router(prices_router)
api_router.include_router(events_router)
api_router.include_router(companies_router)
api_router.include_router(market_data_router)
api_router.include_router(analytics_router)
api_router.include_router(signals_router)
api_router.include_router(screener_router)
api_router.include_router(backtests_router)
api_router.include_router(portfolios_router)

__all__ = ["api_router"]
