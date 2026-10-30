from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from src.core.http_client import make_resilient_session
from src.models.schemas import PropertyListing


class BaseScraper(ABC):
    """
    Abstract interface for marketplace data extraction pipelines.
    Supports standard requests or TLS-impersonating curl_cffi sessions.
    """

    def __init__(self, session: Any | None = None):
        self.session = session or make_resilient_session()

    @abstractmethod
    def fetch_listings_page(self, page_number: int) -> str:
        """Retrieves raw HTML content for a specific pagination index."""
        pass

    @abstractmethod
    def parse_listings(self, html: str) -> Sequence[PropertyListing]:
        """Parses extracted entity cards into validated DTO objects."""
        pass
