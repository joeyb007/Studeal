from __future__ import annotations

from abc import ABC, abstractmethod

from dealbot.schemas import DealRaw


class BaseAdapter(ABC):
    """
    Abstract base for all scraper adapters.
    Each source (Slickdeals, Amazon, Best Buy) implements this interface.
    The pipeline only ever sees DealRaw — source differences are hidden here.
    """

    @abstractmethod
    async def fetch(self) -> list[DealRaw]:
        """Fetch deals from the source and return them normalised as DealRaw."""
        ...
