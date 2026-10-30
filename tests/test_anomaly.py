"""Unit tests for price drop anomaly detection engine and history snapshots."""

import json
from pathlib import Path

from src.main import DEFAULT_BENCHMARK_PRICES, load_price_history, save_price_history
from src.models.schemas import PropertyListing
from src.scrapers.marketplace import MarketplaceCrawler


def test_evaluate_price_drops_significant_drop() -> None:
    """Verifies that a drop exceeding threshold_pct emits a PriceAnomalySignal."""
    crawler = MarketplaceCrawler()
    listing = PropertyListing(
        listing_id=101,
        url="https://example.com/property/101",
        title="Downtown Penthouse",
        price=180000.0,
        area_sqm=75.0,
    )
    # Historical 200,000 -> 10.0% reduction
    signal = crawler.evaluate_price_drops(listing, historical_price=200000.0, threshold_pct=5.0)
    assert signal is not None
    assert signal.listing_id == 101
    assert signal.old_price == 200000.0
    assert signal.new_price == 180000.0
    assert signal.drop_pct == 10.0
    assert signal.absolute_savings == 20000.0


def test_evaluate_price_drops_minor_drop_ignored() -> None:
    """Verifies that price drops below threshold do not trigger alerts."""
    crawler = MarketplaceCrawler()
    listing = PropertyListing(
        listing_id=102,
        url="https://example.com/property/102",
        title="Suburban Villa",
        price=195000.0,
        area_sqm=120.0,
    )
    # Historical 200,000 -> 2.5% reduction (below 5.0% threshold)
    signal = crawler.evaluate_price_drops(listing, historical_price=200000.0, threshold_pct=5.0)
    assert signal is None


def test_evaluate_price_drops_price_increase_ignored() -> None:
    """Verifies that price increases produce no anomaly signal."""
    crawler = MarketplaceCrawler()
    listing = PropertyListing(
        listing_id=103,
        url="https://example.com/property/103",
        title="Modern Loft",
        price=220000.0,
        area_sqm=60.0,
    )
    # Historical 200,000 -> price went UP by 10%
    signal = crawler.evaluate_price_drops(listing, historical_price=200000.0, threshold_pct=5.0)
    assert signal is None


def test_evaluate_price_drops_zero_or_negative_historical() -> None:
    """Verifies that listings without valid positive historical prices are ignored."""
    crawler = MarketplaceCrawler()
    listing = PropertyListing(
        listing_id=104,
        url="https://example.com/property/104",
        title="Studio Apartment",
        price=100000.0,
        area_sqm=30.0,
    )
    assert crawler.evaluate_price_drops(listing, historical_price=0.0) is None
    assert crawler.evaluate_price_drops(listing, historical_price=-5000.0) is None


def test_load_price_history_fallback_benchmark(tmp_path: Path) -> None:
    """Verifies default benchmark seeding when history file does not exist."""
    non_existent = tmp_path / "dumps" / "missing.json"
    history = load_price_history(non_existent)
    assert history == DEFAULT_BENCHMARK_PRICES
    assert 101 in history
    assert history[101] == 215000.0


def test_load_price_history_from_seed_fixture(tmp_path: Path) -> None:
    """Verifies seeding from custom fixture JSON file."""
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps({"501": 350000.0, "502": 420000.0}), encoding="utf-8")

    non_existent = tmp_path / "missing.json"
    history = load_price_history(non_existent, fallback_seed=seed_file)
    assert history == {501: 350000.0, 502: 420000.0}


def test_save_and_reload_price_history_roundtrip(tmp_path: Path) -> None:
    """Verifies disk persistence and reload of extracted prices."""
    history_file = tmp_path / "price_history.json"
    records = [
        PropertyListing(
            listing_id=701,
            url="https://example.com/701",
            title="Apt 701",
            price=150000.0,
            area_sqm=50.0,
        ),
        PropertyListing(
            listing_id=702,
            url="https://example.com/702",
            title="Apt 702",
            price=275000.0,
            area_sqm=90.0,
        ),
    ]

    save_price_history(history_file, records)
    assert history_file.exists()

    loaded = load_price_history(history_file)
    assert loaded == {701: 150000.0, 702: 275000.0}
