"""Unit tests for Pydantic v2 schemas and computed unit economics."""

import pytest
from pydantic import ValidationError

from src.models.schemas import PriceAnomalySignal, PropertyListing
from src.services.gsheets import GSHEETS_COLUMNS, escape_formula_value, listing_to_sheet_row


def test_property_listing_computed_field() -> None:
    """Test price_per_sqm computation and validation."""
    listing = PropertyListing(
        listing_id=1,
        url="https://example.com/prop/1",
        title="Modern Apartment",
        price=100000.0,
        area_sqm=50.0,
        rooms=2,
    )
    assert listing.price_per_sqm == 2000.0
    assert listing.currency == "USD"


def test_property_listing_invalid_price() -> None:
    """Test validation rejection on negative or zero price."""
    with pytest.raises(ValidationError):
        PropertyListing(
            listing_id=1,
            url="https://example.com/prop/1",
            title="Apartment",
            price=-1000.0,
            area_sqm=50.0,
        )


def test_property_listing_floor_boundary_normalization() -> None:
    """Test that if floor exceeds total_floors, total_floors is normalized."""
    listing = PropertyListing(
        listing_id=2,
        url="https://example.com/prop/2",
        title="High Floor Unit",
        price=150000.0,
        area_sqm=60.0,
        floor=15,
        total_floors=10,
    )
    assert listing.floor == 15
    assert listing.total_floors == 15


def test_listing_to_sheet_row_mapping() -> None:
    """Test SRP-compliant Google Sheets serialization."""
    listing = PropertyListing(
        listing_id=123,
        url="https://example.com/prop/123",
        title="Penthouse",
        price=300000.0,
        area_sqm=100.0,
        rooms=3,
        floor=10,
        total_floors=20,
    )
    row = listing_to_sheet_row(listing)
    assert isinstance(row, list)
    assert len(row) == len(GSHEETS_COLUMNS)
    assert row[0] == 123
    assert row[1] == "Penthouse"
    assert row[2] == 300000.0
    assert row[4] == 100.0
    assert row[5] == 3000.0  # price_per_sqm
    assert row[7] == "10/20"


def test_escape_formula_value_injection() -> None:
    """Test defense against Google Sheets formula injection (=, +, -, @)."""
    assert escape_formula_value("=SUM(A1:A10)") == "'=SUM(A1:A10)"
    assert escape_formula_value("+12345") == "'+12345"
    assert escape_formula_value("-100") == "'-100"
    assert escape_formula_value("@command") == "'@command"
    assert escape_formula_value("Normal Title") == "Normal Title"


def test_price_anomaly_signal_computed_savings() -> None:
    """Test absolute savings computation."""
    signal = PriceAnomalySignal(
        listing_id=55,
        url="https://example.com/prop/55",
        old_price=150000.0,
        new_price=135000.0,
        drop_pct=10.0,
    )
    assert signal.absolute_savings == 15000.0
