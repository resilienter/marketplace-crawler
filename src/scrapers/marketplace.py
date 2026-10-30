import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import lxml.html

from src.core.http_client import fetch_page
from src.core.logger import setup_logger
from src.models.schemas import PriceAnomalySignal, PropertyListing
from src.scrapers.base import BaseScraper

log = setup_logger("marketplace_scraper")


def parse_currency(raw_text: str) -> str:
    """Detects ISO currency symbols in scraped price strings."""
    if not raw_text:
        return "USD"
    if "€" in raw_text or "EUR" in raw_text.upper():
        return "EUR"
    if "£" in raw_text or "GBP" in raw_text.upper():
        return "GBP"
    return "USD"


def parse_price(raw_text: str) -> float | None:
    """
    Parses currency strings into floats, handling US and EU formatting,
    rental suffixes ($1,200/mo.), and leading decimal points (.500).
    """
    if not raw_text or not raw_text.strip():
        return None

    # Strip characters other than digits, dots, commas, then remove leading/trailing punctuation
    cleaned = re.sub(r"[^\d.,]", "", raw_text.strip()).strip(".,")
    if not cleaned or not any(c.isdigit() for c in cleaned):
        return None

    # Handle leading decimal like .500 -> 0.500
    if cleaned.startswith("."):
        cleaned = "0" + cleaned
    elif cleaned.startswith(","):
        cleaned = "0." + cleaned[1:]

    # Both dot and comma present
    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            # European format: 195.000,50 -> 195000.50
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # US format: 195,000.50 -> 195000.50
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Only comma present: 195,000 (thousand) or 195,50 (decimal)
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        # Only dot present: 195.000 (thousand) or 195.50 (decimal)
        parts = cleaned.split(".")
        if len(parts) > 2 or (
            len(parts) == 2 and len(parts[1]) == 3 and parts[0].isdigit() and int(parts[0]) > 0
        ):
            cleaned = "".join(parts)

    try:
        val = float(cleaned)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def parse_area(card: Any) -> float | None:
    """
    Extracts floor area in square meters from dedicated tags or card text.
    Rejects malformed data and avoids arbitrary synthetic defaults.
    """
    area_nodes = card.xpath(
        ".//span[contains(@class, 'area') or contains(@class, 'size') or contains(., 'sqm') or contains(., 'm²')]//text()"
    )
    search_text = " ".join("".join(area_nodes).split())

    if not search_text:
        search_text = " ".join("".join(card.xpath(".//text()")).split())

    match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:m²|sqm|sq\.?\s*m|square\s+meters)", search_text, re.IGNORECASE
    )
    if not match:
        return None

    num_str = match.group(1).replace(",", ".")
    try:
        area = float(num_str)
        return area if area > 0 else None
    except (ValueError, TypeError):
        return None


class MarketplaceCrawler(BaseScraper):
    """Fetches catalog pages, parses listing cards, flags price drops."""

    def __init__(
        self,
        base_url: str = "https://example-realestate-marketplace.com",
        session: Any | None = None,
    ):
        super().__init__(session=session)
        self.base_url = base_url.rstrip("/")

    def fetch_listings_page(self, page_number: int) -> str:
        url = f"{self.base_url}/properties?page={page_number}&sort=newest"
        return fetch_page(self.session, url, label=f"CATALOG:P{page_number}")

    def parse_listings(self, html: str) -> list[PropertyListing]:
        """Extracts structured property listings from HTML markup."""
        listings: list[PropertyListing] = []
        if not html or not html.strip():
            return listings

        try:
            doc = lxml.html.fromstring(html)
        except Exception as err:
            log.warning("Failed to parse HTML document into DOM: %s", err)
            return listings

        cards = doc.xpath(
            "//article[contains(@class, 'property-card')] | //div[@data-name='ListingCard']"
        )
        if not cards:
            log.warning("No property cards found matching target XPath selectors in HTML payload")
            return listings

        for card in cards:
            try:
                link_nodes = card.xpath(".//a[contains(@href, '/property/')]/@href")
                if not link_nodes:
                    continue
                raw_url = link_nodes[0]
                canonical_url = urljoin(self.base_url, raw_url)

                listing_id_match = re.search(r"/property/(\d+)", canonical_url)
                if not listing_id_match:
                    continue
                listing_id = int(listing_id_match.group(1))

                title_nodes = card.xpath(".//h3//text() | .//a[contains(@class, 'title')]//text()")
                title = " ".join("".join(title_nodes).split()) or f"Property #{listing_id}"

                price_nodes = card.xpath(".//span[contains(@class, 'price')]//text()")
                price_text = "".join(price_nodes)
                price = parse_price(price_text)
                if price is None:
                    continue
                currency = parse_currency(price_text)

                area_sqm = parse_area(card)
                if area_sqm is None:
                    log.debug("Skipping listing %d: missing valid area measurement", listing_id)
                    continue

                card_text = " ".join("".join(card.xpath(".//text()")).split())
                rooms_match = re.search(r"(\d+)\s*(?:room|bed|br)", card_text, re.IGNORECASE)
                rooms = int(rooms_match.group(1)) if rooms_match else 1

                floor_match = re.search(r"(\d+)\s*/\s*(\d+)\s*fl", card_text, re.IGNORECASE)
                floor = int(floor_match.group(1)) if floor_match else 1
                total_floors = int(floor_match.group(2)) if floor_match else max(floor, 1)

                listing = PropertyListing(
                    listing_id=listing_id,
                    url=canonical_url,
                    title=title,
                    price=price,
                    currency=currency,
                    area_sqm=area_sqm,
                    rooms=rooms,
                    floor=floor,
                    total_floors=total_floors,
                    scraped_at=datetime.now(UTC),
                )
                listings.append(listing)

            except Exception as err:
                log.warning(
                    "Skipped unparseable card #%s due to schema mismatch: %s",
                    getattr(card, "tag", "?"),
                    err,
                )
                continue

        return listings

    def evaluate_price_drops(
        self, current_listing: PropertyListing, historical_price: float, threshold_pct: float = 5.0
    ) -> PriceAnomalySignal | None:
        """Flags price drop events that exceed threshold_pct."""
        if historical_price <= 0 or current_listing.price >= historical_price:
            return None

        drop_pct = round(((historical_price - current_listing.price) / historical_price) * 100.0, 2)
        if drop_pct >= threshold_pct:
            return PriceAnomalySignal(
                listing_id=current_listing.listing_id,
                url=current_listing.url,
                old_price=historical_price,
                new_price=current_listing.price,
                drop_pct=drop_pct,
            )

        return None
