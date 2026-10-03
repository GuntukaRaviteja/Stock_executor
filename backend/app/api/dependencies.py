from typing import Generator
from fastapi import Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.services.company_service import CompanyService, company_service
from app.services.event_service import EventService, event_service
from app.services.ingestion_service import IngestionService, ingestion_service
from app.services.market_data import market_data_service
from app.services.price_service import PriceService, price_service
from app.services.stock_service import StockService, stock_service
from app.services.analytics_service import AnalyticsService, analytics_service
from app.services.signal_service import SignalService, signal_service
from app.services.screener_service import ScreenerService, screener_service
from app.services.backtest import BacktestService, backtest_service
from app.services.portfolio import (
    PaperTradingEngine,
    PaperTradingService,
    PortfolioAllocationService,
    PortfolioConstructionService,
    StrategyOptimizationService,
    paper_trading_engine,
    paper_trading_service,
    portfolio_allocation_service,
    portfolio_construction_service,
    strategy_optimization_service,
)
from app.services.portfolio_risk import (
    PortfolioRiskService,
    portfolio_risk_service,
)


def get_stock_service() -> StockService:
    return stock_service


def get_price_service() -> PriceService:
    return price_service


def get_event_service() -> EventService:
    return event_service


def get_company_service() -> CompanyService:
    return company_service


def get_ingestion_service() -> IngestionService:
    return ingestion_service


def get_market_data_service():
    return market_data_service


def get_analytics_service() -> AnalyticsService:
    return analytics_service


def get_signal_service() -> SignalService:
    return signal_service


def get_screener_service() -> ScreenerService:
    return screener_service


def get_backtest_service() -> BacktestService:
    return backtest_service


def get_paper_trading_service() -> PaperTradingService:
    return paper_trading_service


def get_paper_trading_engine() -> PaperTradingEngine:
    return paper_trading_engine


def get_portfolio_construction_service() -> PortfolioConstructionService:
    return portfolio_construction_service


def get_portfolio_allocation_service() -> PortfolioAllocationService:
    return portfolio_allocation_service


def get_strategy_optimization_service() -> StrategyOptimizationService:
    return strategy_optimization_service


def get_portfolio_risk_service() -> PortfolioRiskService:
    return portfolio_risk_service
