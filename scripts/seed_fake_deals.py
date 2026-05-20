#!/usr/bin/env python3
"""Insert fake deals for local UI development. Run with: python scripts/seed_fake_deals.py"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from dealbot.db.models import Base, Deal

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/dealbot")

FAKE_DEALS = [
    {
        "title": "Apple AirPods Pro (2nd Gen) with USB-C — Active Noise Cancellation",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE1",
        "listed_price": 329.00,
        "sale_price": 199.00,
        "score": 88,
        "alert_tier": "push",
        "category": "Audio",
        "real_discount_pct": 39.5,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "Samsung 49\" Odyssey G9 DQHD 240Hz Curved Gaming Monitor",
        "source": "eBay",
        "url": "https://ebay.ca/itm/EXAMPLE2",
        "listed_price": 1799.99,
        "sale_price": 989.00,
        "score": 81,
        "alert_tier": "push",
        "category": "Electronics",
        "real_discount_pct": 45.1,
        "student_eligible": False,
        "condition": "refurb",
    },
    {
        "title": "Logitech MX Master 3S Wireless Mouse — Quiet Clicks",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE3",
        "listed_price": 129.99,
        "sale_price": 79.99,
        "score": 74,
        "alert_tier": "digest",
        "category": "Accessories",
        "real_discount_pct": 38.5,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "ASUS ROG Zephyrus G14 — Ryzen 9 / RTX 4060 / 16GB RAM / 1TB SSD",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE4",
        "listed_price": 1899.00,
        "sale_price": 1299.00,
        "score": 85,
        "alert_tier": "push",
        "category": "Laptops",
        "real_discount_pct": 31.6,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "Sony WH-1000XM5 Wireless Noise Cancelling Headphones — Midnight Blue",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE5",
        "listed_price": 449.99,
        "sale_price": 278.00,
        "score": 79,
        "alert_tier": "digest",
        "category": "Audio",
        "real_discount_pct": 38.2,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "iPad Air 11\" M2 — 256GB WiFi — Blue",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE6",
        "listed_price": 929.00,
        "sale_price": 749.00,
        "score": 71,
        "alert_tier": "digest",
        "category": "Tablets",
        "real_discount_pct": 19.4,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "SteelSeries Arctis Nova Pro Wireless Gaming Headset",
        "source": "eBay",
        "url": "https://ebay.ca/itm/EXAMPLE7",
        "listed_price": 379.99,
        "sale_price": 189.00,
        "score": 77,
        "alert_tier": "digest",
        "category": "Gaming",
        "real_discount_pct": 50.3,
        "student_eligible": False,
        "condition": "used",
    },
    {
        "title": "Kindle Paperwhite Signature Edition — 32GB, Wireless Charging",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE8",
        "listed_price": 249.99,
        "sale_price": 159.99,
        "score": 66,
        "alert_tier": "digest",
        "category": "Electronics",
        "real_discount_pct": 36.0,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "Anker 737 Power Bank — 24,000mAh, 140W Fast Charging",
        "source": "Amazon",
        "url": "https://amazon.ca/dp/EXAMPLE9",
        "listed_price": 159.99,
        "sale_price": 89.99,
        "score": 62,
        "alert_tier": "none",
        "category": "Accessories",
        "real_discount_pct": 43.8,
        "student_eligible": True,
        "condition": "new",
    },
    {
        "title": "Microsoft Surface Laptop 5 — 13.5\" Touch, i5, 8GB, 512GB",
        "source": "eBay",
        "url": "https://ebay.ca/itm/EXAMPLE10",
        "listed_price": 1699.00,
        "sale_price": 849.00,
        "score": 83,
        "alert_tier": "push",
        "category": "Laptops",
        "real_discount_pct": 50.0,
        "student_eligible": False,
        "condition": "refurb",
    },
]


async def seed():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        now = datetime.now(timezone.utc)
        today = date.today()
        for d in FAKE_DEALS:
            deal = Deal(
                title=d["title"],
                source=d["source"],
                url=d["url"],
                listed_price=d["listed_price"],
                sale_price=d["sale_price"],
                asin=None,
                score=d["score"],
                alert_tier=d["alert_tier"],
                category=d["category"],
                tags=json.dumps([]),
                confidence="high",
                real_discount_pct=d["real_discount_pct"],
                student_eligible=d["student_eligible"],
                condition=d["condition"],
                affiliate_url=None,
                embedding=None,
                hunt_date=today,
                first_seen_at=now,
                scraped_at=now,
            )
            session.add(deal)
        await session.commit()
        print(f"Seeded {len(FAKE_DEALS)} fake deals.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
