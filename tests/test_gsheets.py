"""Unit tests for GoogleSheetsWriter, formula escaping, and idempotent sync."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.models.schemas import PropertyListing
from src.services.gsheets import (
    GoogleSheetsWriter,
    are_values_equal,
    escape_formula_value,
    listing_to_sheet_row,
    row_data_changed,
)


def test_escape_formula_value_injection() -> None:
    """Verifies formula injection characters are escaped with leading single quote."""
    assert escape_formula_value("=SUM(A1:A10)") == "'=SUM(A1:A10)"
    assert escape_formula_value("+cmd|'/C calc'!A0") == "'+cmd|'/C calc'!A0"
    assert escape_formula_value("-100") == "'-100"
    assert escape_formula_value("@dangerous") == "'@dangerous"
    assert escape_formula_value("Standard Title") == "Standard Title"
    assert escape_formula_value(195000) == 195000
    assert escape_formula_value("") == ""


def test_listing_to_sheet_row_mapping() -> None:
    """Verifies DTO serialization from PropertyListing to 12 spreadsheet columns."""
    listing = PropertyListing(
        listing_id=101,
        url="https://example.com/property/101",
        title="=Malicious Penthouse",
        price=195000.0,
        currency="USD",
        area_sqm=42.5,
        rooms=1,
        floor=5,
        total_floors=12,
        scraped_at=datetime(2026, 10, 27, 20, 0, 0, tzinfo=UTC),
    )
    row = listing_to_sheet_row(listing)
    assert len(row) == 12
    assert row[0] == 101
    assert row[1] == "'=Malicious Penthouse"  # Escaped
    assert row[2] == 195000.0
    assert row[3] == "USD"
    assert row[4] == 42.5
    assert row[5] == round(195000.0 / 42.5, 2)
    assert row[6] == 1
    assert row[7] == "5/12"
    assert row[10] == "https://example.com/property/101"
    assert row[11] == "2026-10-27 20:00:00"


def test_are_values_equal() -> None:
    """Verifies cell value comparison across formats (int, float, comma decimals)."""
    assert are_values_equal("101", 101)
    assert are_values_equal("195000", 195000.0)
    assert are_values_equal("195000,5", 195000.5)
    assert are_values_equal("45", 45.0)
    assert are_values_equal("USD", "USD")
    assert are_values_equal("4/12", "4/12")
    assert not are_values_equal("195000", 180000.0)
    assert not are_values_equal("USD", "EUR")


def test_row_data_changed_ignores_timestamp_column() -> None:
    """Verifies that changing ONLY column 11 (scraped_at) does not count as data change."""
    old_row = [
        101,
        "Downtown Studio",
        195000.0,
        "USD",
        42.5,
        4588.24,
        1,
        "5/12",
        0,
        0,
        "https://example.com/101",
        "2026-10-26 12:00:00",
    ]
    new_row = [
        101,
        "Downtown Studio",
        195000.0,
        "USD",
        42.5,
        4588.24,
        1,
        "5/12",
        0,
        0,
        "https://example.com/101",
        "2026-10-27 18:30:00",  # Timestamp changed!
    ]
    assert not row_data_changed(old_row, new_row)

    # When price changes, change is detected
    modified_price_row = list(new_row)
    modified_price_row[2] = 185000.0
    assert row_data_changed(old_row, modified_price_row)


@patch("src.services.gsheets._HAS_GOOGLE_API", True)
def test_sync_listings_idempotent(tmp_path) -> None:
    """Verifies that repeated sync of identical data results in 0 inserted, 0 updated, all unchanged."""
    mock_sa = tmp_path / "sa.json"
    mock_sa.write_text("{}", encoding="utf-8")

    writer = GoogleSheetsWriter(
        spreadsheet_id="test_sheet_id",
        service_account_path=mock_sa,
    )

    existing_sheet_data = [
        ["Listing ID", "Title", "Price", "Currency"],
        [
            "101",
            "Downtown Studio",
            "195000",
            "USD",
            "42.5",
            "4588.24",
            "1",
            "5/12",
            "0",
            "0",
            "http://101",
        ],
    ]

    mock_service = MagicMock()
    # Mock values().get
    mock_service.spreadsheets().values().get().execute.return_value = {
        "values": existing_sheet_data
    }
    # Mock spreadsheets().get for sheet existence
    mock_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "ActiveListings"}}]
    }
    writer._service = mock_service

    incoming = [
        [
            101,
            "Downtown Studio",
            195000.0,
            "USD",
            42.5,
            4588.24,
            1,
            "5/12",
            0,
            0,
            "http://101",
            "2026-10-27",
        ],
    ]

    inserted, updated, unchanged = writer.sync_listings("ActiveListings", incoming)
    assert inserted == 0
    assert updated == 0
    assert unchanged == 1
