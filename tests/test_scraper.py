"""Unit tests for MarketplaceCrawler DOM parsing, price cleaning, and anomaly detection."""

from unittest.mock import MagicMock

from src.models.schemas import PropertyListing
from src.scrapers.marketplace import (
    MarketplaceCrawler,
    parse_area,
    parse_currency,
    parse_price,
)
from src.services.telegram import redact_telegram_token

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<body>
    <div class="property-card" data-name="ListingCard">
        <a href="/property/501" class="title"><h3>Sunny Central Penthouse</h3></a>
        <span class="price">$195,000</span>
        <span class="area">85.5 sqm</span>
        <div class="info">3 rooms • 7/10 fl</div>
    </div>
    <div class="property-card" data-name="ListingCard">
        <a href="https://example-realestate-marketplace.com/property/502" class="title"><h3>Suburban Townhouse</h3></a>
        <span class="price">€240.000,50</span>
        <span class="area">120,0 m²</span>
        <div class="info">4 beds • 1/2 fl</div>
    </div>
    <!-- Malformed card without link -->
    <div class="property-card">
        <span class="price">$50,000</span>
    </div>
</body>
</html>
"""


def test_parse_listings_success() -> None:
    """Test extracting multiple valid cards while skipping malformed cards."""
    crawler = MarketplaceCrawler()
    listings = crawler.parse_listings(SAMPLE_HTML)

    assert len(listings) == 2

    l1 = listings[0]
    assert l1.listing_id == 501
    assert l1.title == "Sunny Central Penthouse"
    assert l1.price == 195000.0
    assert l1.currency == "USD"
    assert l1.area_sqm == 85.5
    assert l1.rooms == 3
    assert l1.floor == 7
    assert l1.total_floors == 10
    assert l1.price_per_sqm == round(195000.0 / 85.5, 2)
    assert l1.url == "https://example-realestate-marketplace.com/property/501"

    l2 = listings[1]
    assert l2.listing_id == 502
    assert l2.price == 240000.50
    assert l2.currency == "EUR"
    assert l2.area_sqm == 120.0
    assert l2.rooms == 4
    # Verify absolute URL retains correct path without double-prefixing
    assert l2.url == "https://example-realestate-marketplace.com/property/502"


def test_parse_listings_empty_html() -> None:
    """Test that empty or blank HTML returns an empty list without raising exceptions."""
    crawler = MarketplaceCrawler()
    assert crawler.parse_listings("") == []
    assert crawler.parse_listings("   ") == []


def test_parse_price_formats_and_edge_cases() -> None:
    """Test parsing diverse international price representations and adversarial edge cases."""
    assert parse_price("$195,000") == 195000.0
    assert parse_price("$195,000.50") == 195000.50
    assert parse_price("195.000,50 €") == 195000.50
    assert parse_price("195.000") == 195000.0
    assert parse_price("85 000 USD") == 85000.0
    assert parse_price("$1,200.00/mo.") == 1200.0  # Trailing dot handling
    assert parse_price(".500") == 500.0  # Safe from ValueError
    assert parse_price("£320,000") == 320000.0
    assert parse_price("") is None
    assert parse_price("Contact agent") is None
    assert parse_price(".") is None
    assert parse_price("..") is None


def test_parse_currency() -> None:
    """Test currency detection across international listings."""
    assert parse_currency("$500,000") == "USD"
    assert parse_currency("€195.000") == "EUR"
    assert parse_currency("195 000 EUR") == "EUR"
    assert parse_currency("£285,000") == "GBP"
    assert parse_currency("285,000 GBP") == "GBP"


def test_telegram_token_redaction() -> None:
    """Checks sensitive bot tokens are masked in exception logs."""
    raw_error = "HTTPError: 404 for url: https://api.telegram.org/bot123456789:AAEk92_SecretKey123/sendMessage"
    masked = redact_telegram_token(raw_error)
    assert "AAEk92_SecretKey123" not in masked
    assert "bot[REDACTED]" in masked


def test_parse_area_fallback_and_comma() -> None:
    """Test parsing square meters with commas and missing nodes."""
    import lxml.html

    doc1 = lxml.html.fromstring("<div><span class='size'>75,5 m²</span></div>")
    assert parse_area(doc1) == 75.5

    doc2 = lxml.html.fromstring("<div><p>Spacious living: 110 sqm available</p></div>")
    assert parse_area(doc2) == 110.0

    doc3 = lxml.html.fromstring("<div><p>No area mentioned</p></div>")
    assert parse_area(doc3) is None


def test_custom_session_injection() -> None:
    """Checks MarketplaceCrawler accepts custom injected HTTP session."""
    mock_session = MagicMock()
    crawler = MarketplaceCrawler(session=mock_session)
    assert crawler.session is mock_session


def test_evaluate_price_drops_signal_detected() -> None:
    """Test detection of significant price reduction."""
    crawler = MarketplaceCrawler()
    listing = PropertyListing(
        listing_id=901,
        url="https://example.com/property/901",
        title="Luxury Villa",
        price=180000.0,
        area_sqm=150.0,
    )
    # Historical price was 200,000 -> 10% drop
    signal = crawler.evaluate_price_drops(listing, historical_price=200000.0, threshold_pct=5.0)

    assert signal is not None
    assert signal.listing_id == 901
    assert signal.old_price == 200000.0
    assert signal.new_price == 180000.0
    assert signal.drop_pct == 10.0
    assert signal.absolute_savings == 20000.0


def test_evaluate_price_drops_no_signal_on_small_change() -> None:
    """Test small drop or price increase produces no signal."""
    crawler = MarketplaceCrawler()
    listing = PropertyListing(
        listing_id=902,
        url="https://example.com/property/902",
        title="Studio",
        price=100000.0,
        area_sqm=40.0,
    )
    # Historical 102,000 -> < 2% drop
    signal = crawler.evaluate_price_drops(listing, historical_price=102000.0, threshold_pct=5.0)
    assert signal is None

    # Price went up
    signal_up = crawler.evaluate_price_drops(listing, historical_price=95000.0, threshold_pct=5.0)
    assert signal_up is None
