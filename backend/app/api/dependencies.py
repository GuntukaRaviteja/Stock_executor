from typing import Generator
from fastapi import Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.services.company_service import CompanyService, company_service
from app.services.event_service import EventService, event_service
from app.services.ingestion_service import IngestionService, ingestion_service
from app.services.price_service import PriceService, price_service
from app.services.stock_service import StockService, stock_service


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
