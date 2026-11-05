"""License names and estimated prices (see data/license_prices.yaml)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PRICES_FILE = Path(__file__).parent / "data" / "license_prices.yaml"


@lru_cache(maxsize=1)
def _table() -> dict:
    return yaml.safe_load(PRICES_FILE.read_text())


def sku_name(part_number: str) -> str:
    info = _table()["skus"].get(part_number)
    return info["name"] if info else part_number.replace("_", " ").title()


def sku_price(part_number: str) -> float | None:
    """Estimated monthly price per user in USD. 0 for free products, None if we don't know."""
    info = _table()["skus"].get(part_number)
    return float(info["price"]) if info else None


def prices_reviewed() -> str:
    return str(_table()["reviewed"])
