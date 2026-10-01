from typing import List
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_company_service, get_db
from app.schemas.company import CompanyResponse, CompanyUpdate
from app.services.company_service import CompanyService

router = APIRouter(prefix="/companies", tags=["Companies"])


@router.get(
    "",
    response_model=List[CompanyResponse],
    summary="List all company profiles",
    description="Retrieve all stored company background and fundamental profiles.",
    status_code=status.HTTP_200_OK,
)
def list_companies(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    service: CompanyService = Depends(get_company_service),
) -> List[CompanyResponse]:
    companies = service.list_companies(db, skip=skip, limit=limit)
    res = []
    for c in companies:
        item = CompanyResponse.model_validate(c)
        if c.stock:
            item.symbol = c.stock.symbol
            item.company_name = c.stock.company_name
            item.exchange = c.stock.exchange
        res.append(item)
    return res


@router.get(
    "/symbol/{symbol}",
    response_model=CompanyResponse,
    summary="Get company profile by stock symbol",
    description="Retrieve comprehensive company profile and overview by ticker symbol.",
    status_code=status.HTTP_200_OK,
)
def get_company_by_symbol(
    symbol: str,
    db: Session = Depends(get_db),
    service: CompanyService = Depends(get_company_service),
) -> CompanyResponse:
    company = service.get_by_symbol(db, symbol)
    item = CompanyResponse.model_validate(company)
    if company.stock:
        item.symbol = company.stock.symbol
        item.company_name = company.stock.company_name
        item.exchange = company.stock.exchange
    return item


@router.get(
    "/{stock_id}",
    response_model=CompanyResponse,
    summary="Get company profile by stock ID",
    description="Retrieve company profile details by stock ID.",
    status_code=status.HTTP_200_OK,
)
def get_company_by_stock_id(
    stock_id: int,
    db: Session = Depends(get_db),
    service: CompanyService = Depends(get_company_service),
) -> CompanyResponse:
    company = service.get_by_stock_id(db, stock_id)
    item = CompanyResponse.model_validate(company)
    if company.stock:
        item.symbol = company.stock.symbol
        item.company_name = company.stock.company_name
        item.exchange = company.stock.exchange
    return item


@router.put(
    "/{stock_id}",
    response_model=CompanyResponse,
    summary="Update or create company profile",
    description="Update company fundamentals (sector, industry, CEO, employees, market cap).",
    status_code=status.HTTP_200_OK,
)
def update_company(
    stock_id: int,
    company_in: CompanyUpdate,
    db: Session = Depends(get_db),
    service: CompanyService = Depends(get_company_service),
) -> CompanyResponse:
    company = service.update_company(db, stock_id, company_in)
    item = CompanyResponse.model_validate(company)
    if company.stock:
        item.symbol = company.stock.symbol
        item.company_name = company.stock.company_name
        item.exchange = company.stock.exchange
    return item
