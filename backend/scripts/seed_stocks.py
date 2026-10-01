import os
import sys

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import SessionLocal
from app.core.logging import logger
from app.models.company import Company
from app.models.stock import Stock

INITIAL_STOCKS = [
    {
        "symbol": "AAPL",
        "company_name": "Apple Inc.",
        "exchange": "NASDAQ",
        "profile": {
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "description": "Apple Inc. designs, manufactures, and markets smartphones, personal computers, tablets, wearables, and accessories.",
            "ceo": "Tim Cook",
            "website": "https://www.apple.com",
            "headquarters": "Cupertino, California, USA",
            "employees": 161000,
            "market_cap": 3450000000000.00,
        },
    },
    {
        "symbol": "MSFT",
        "company_name": "Microsoft Corporation",
        "exchange": "NASDAQ",
        "profile": {
            "sector": "Technology",
            "industry": "Software—Infrastructure",
            "description": "Microsoft Corporation develops and supports software, services, devices, and solutions worldwide.",
            "ceo": "Satya Nadella",
            "website": "https://www.microsoft.com",
            "headquarters": "Redmond, Washington, USA",
            "employees": 221000,
            "market_cap": 3150000000000.00,
        },
    },
    {
        "symbol": "GOOGL",
        "company_name": "Alphabet Inc.",
        "exchange": "NASDAQ",
        "profile": {
            "sector": "Communication Services",
            "industry": "Internet Content & Information",
            "description": "Alphabet Inc. offers search, cloud computing, software, hardware, and advertising platforms.",
            "ceo": "Sundar Pichai",
            "website": "https://abc.xyz",
            "headquarters": "Mountain View, California, USA",
            "employees": 182000,
            "market_cap": 2180000000000.00,
        },
    },
    {
        "symbol": "AMZN",
        "company_name": "Amazon.com Inc.",
        "exchange": "NASDAQ",
        "profile": {
            "sector": "Consumer Cyclical",
            "industry": "Internet Retail",
            "description": "Amazon.com, Inc. focuses on retail sale of consumer products, subscriptions, and cloud infrastructure.",
            "ceo": "Andy Jassy",
            "website": "https://www.amazon.com",
            "headquarters": "Seattle, Washington, USA",
            "employees": 1525000,
            "market_cap": 1950000000000.00,
        },
    },
    {
        "symbol": "NVDA",
        "company_name": "NVIDIA Corporation",
        "exchange": "NASDAQ",
        "profile": {
            "sector": "Technology",
            "industry": "Semiconductors",
            "description": "NVIDIA Corporation designs graphics processing units (GPUs) and system on a chip units (SoCs).",
            "ceo": "Jensen Huang",
            "website": "https://www.nvidia.com",
            "headquarters": "Santa Clara, California, USA",
            "employees": 29600,
            "market_cap": 3100000000000.00,
        },
    },
    {
        "symbol": "TSLA",
        "company_name": "Tesla, Inc.",
        "exchange": "NASDAQ",
        "profile": {
            "sector": "Consumer Cyclical",
            "industry": "Auto Manufacturers",
            "description": "Tesla, Inc. designs, develops, manufactures, sells, and leases electric vehicles and energy storage systems.",
            "ceo": "Elon Musk",
            "website": "https://www.tesla.com",
            "headquarters": "Austin, Texas, USA",
            "employees": 140473,
            "market_cap": 780000000000.00,
        },
    },
]


def seed_stocks():
    """Seed initial stocks and company profiles idempotently."""
    db = SessionLocal()
    inserted_stocks = 0
    skipped_stocks = 0

    print("Starting stock database seeding...")
    try:
        for item in INITIAL_STOCKS:
            symbol = item["symbol"].upper()
            existing = db.query(Stock).filter(Stock.symbol == symbol).first()

            if existing:
                print(f"  {symbol}: already exists (id={existing.id}), skipping stock creation")
                skipped_stocks += 1
                stock = existing
            else:
                stock = Stock(
                    symbol=symbol,
                    company_name=item["company_name"],
                    exchange=item["exchange"],
                )
                db.add(stock)
                db.commit()
                db.refresh(stock)
                inserted_stocks += 1
                print(f"  {symbol}: inserted new stock (id={stock.id})")

            # Check / upsert company profile
            profile_data = item.get("profile")
            if profile_data:
                comp = db.query(Company).filter(Company.stock_id == stock.id).first()
                if not comp:
                    comp = Company(stock_id=stock.id, **profile_data)
                    db.add(comp)
                    db.commit()
                    print(f"    - Attached company profile for {symbol}")

        print(f"\nSeeding completed successfully!")
        print(f"Summary: {inserted_stocks} inserted, {skipped_stocks} skipped.\n")
    except Exception as e:
        db.rollback()
        logger.exception(f"Seeding failed: {e}")
        print(f"ERROR: Seeding failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    seed_stocks()
