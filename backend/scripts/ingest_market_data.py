import argparse
import asyncio
import os
import sys

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import SessionLocal
from app.core.logging import logger
from app.services.ingestion_service import ingestion_service


async def main():
    parser = argparse.ArgumentParser(description="Ingest historical market data and events into MySQL.")
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Specific stock symbols to ingest (default: all stocks in DB)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of historical days to fetch (default: 30)",
    )
    parser.add_argument(
        "--no-events",
        action="store_true",
        help="Skip fetching corporate news/events",
    )

    args = parser.parse_args()

    db = SessionLocal()
    print("Starting ingestion...\n")

    try:
        results = await ingestion_service.run_pipeline(
            db=db,
            symbols=args.symbols,
            days=args.days,
            include_events=not args.no_events,
        )

        for symbol, stat in results.items():
            print(f"{symbol}:")
            print(f"  fetched: {stat.get('fetched', 0)}")
            print(f"  inserted: {stat.get('inserted', 0)}")
            print(f"  skipped: {stat.get('skipped', 0)}")
            if stat.get("status") == "failed":
                print(f"  error: {stat.get('error')}")
            print()

        print("Ingestion completed.")
    except Exception as e:
        logger.exception(f"Fatal error during ingestion pipeline: {e}")
        print(f"Ingestion failed with error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
