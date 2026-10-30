import contextlib
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    _HAS_GOOGLE_API = True
except ImportError:
    _HAS_GOOGLE_API = False
    Credentials = None  # type: ignore
    build = None  # type: ignore
    HttpError = None  # type: ignore

from src.config import settings
from src.core.logger import setup_logger
from src.models.schemas import PropertyListing

log = setup_logger("gsheets", settings.logs_dir / "crawler.log")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_ROW_COUNT = 50000
DEFAULT_COL_COUNT = 26

GSHEETS_COLUMNS: list[str] = [
    "Listing ID",
    "Title",
    "Price",
    "Currency",
    "Area (sqm)",
    "Price / sqm",
    "Rooms",
    "Floor / Total",
    "Views Today",
    "Views Total",
    "URL",
    "Scraped At",
]


def escape_formula_value(val: Any) -> Any:
    """Escape leading =, +, -, or @ with a single quote to prevent spreadsheet formula execution."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@"):
        return f"'{val}"
    return val


def listing_to_sheet_row(item: PropertyListing) -> list[Any]:
    return [
        item.listing_id,
        escape_formula_value(item.title),
        item.price,
        item.currency,
        item.area_sqm,
        item.price_per_sqm,
        item.rooms,
        f"{item.floor}/{item.total_floors}",
        item.views_today or 0,
        item.views_total or 0,
        item.url,
        item.scraped_at.strftime("%Y-%m-%d %H:%M:%S"),
    ]


def are_values_equal(val_old: Any, val_new: Any) -> bool:
    """
    Compares sheet cell strings with model attributes.
    Normalizes floats, integers, and comma decimal separators.
    """
    s_old = str(val_old).strip() if val_old is not None else ""
    s_new = str(val_new).strip() if val_new is not None else ""
    if s_old == s_new:
        return True
    try:
        num_old = float(s_old.replace(",", "."))
        num_new = float(s_new.replace(",", "."))
        return abs(num_old - num_new) < 0.001
    except (ValueError, TypeError):
        pass
    return False


def row_data_changed(old_r: list[Any], new_r: list[Any], compare_cols: int = 11) -> bool:
    """
    Compares data fields between sheet row and newly scraped row.
    Defaults to columns 0..10, omitting Column 11 ('Scraped At' timestamp)
    so identical listings are recognized as unchanged without updating every cycle.
    """
    limit = min(compare_cols, len(new_r))
    for i in range(limit):
        old_val = old_r[i] if i < len(old_r) else ""
        new_val = new_r[i]
        if not are_values_equal(old_val, new_val):
            return True
    return False


class GoogleSheetsWriter:
    """
    Writes property listings to Google Sheets in batches.
    Handles upsert by listing ID, RAW cell formatting, and exponential backoff on retries.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        service_account_path: Path | str,
        batch_size: int = 300,
        retries: int = 5,
        backoff_cap: float = 20.0,
    ):
        self.spreadsheet_id = spreadsheet_id
        self._gsa_path = Path(service_account_path)
        self.batch_size = batch_size
        self.retries = retries
        self.backoff_cap = backoff_cap

        self._sheet_props_cache: dict[str, dict[str, int]] = {}
        self._service: Any = None
        self._build_service()

    def _build_service(self) -> None:
        if not _HAS_GOOGLE_API:
            log.warning("googleapiclient is not installed. Google Sheets export disabled.")
            self._service = None
            return

        if not self._gsa_path.exists():
            log.warning("Google service account file not found: %s", self._gsa_path)
            self._service = None
            return

        try:
            creds = Credentials.from_service_account_file(str(self._gsa_path), scopes=SCOPES)
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        except Exception as exc:
            log.warning("Failed to authenticate Google Sheets service account: %s", exc)
            self._service = None

    def _sleep_backoff(self, attempt: int, base: float = 1.0) -> None:
        wait = min(self.backoff_cap, base * (2.0 ** (attempt - 1))) + random.uniform(0.0, 1.0)
        time.sleep(wait)

    def _execute_api_call(self, action: Callable[[Any], Any]) -> Any:
        if self._service is None:
            self._build_service()
            if self._service is None:
                raise RuntimeError(
                    "Google Sheets service unavailable (credentials missing or invalid)"
                )

        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = action(self._service)
                return request.execute()

            except Exception as err:
                last_err = err
                status = getattr(getattr(err, "resp", None), "status", None)
                if status in (429, 500, 502, 503, 504):
                    log.warning(
                        "Google API rate limit/server error HTTP %s. Retrying attempt %d/%d",
                        status,
                        attempt,
                        self.retries,
                    )
                    self._sleep_backoff(attempt)
                    continue

                if isinstance(err, (TimeoutError, ConnectionError, OSError)):
                    with contextlib.suppress(Exception):
                        self._build_service()
                    self._sleep_backoff(attempt)
                    continue

                raise

        raise RuntimeError(
            f"Google Sheets API call failed after {self.retries} attempts: {last_err}"
        ) from last_err

    def ensure_sheet_exists(self, tab_title: str) -> bool:
        meta = (
            self._execute_api_call(
                lambda svc: svc.spreadsheets().get(spreadsheetId=self.spreadsheet_id)
            )
            or {}
        )

        for sheet in meta.get("sheets", []):
            if sheet.get("properties", {}).get("title") == tab_title:
                return False

        body = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": tab_title,
                            "gridProperties": {
                                "rowCount": DEFAULT_ROW_COUNT,
                                "columnCount": DEFAULT_COL_COUNT,
                            },
                        }
                    }
                }
            ]
        }
        self._execute_api_call(
            lambda svc: svc.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body)
        )
        return True

    def ensure_sheet_headers(self, tab_title: str, headers: list[str]) -> None:
        if not headers:
            return
        body = {"values": [headers]}
        self._execute_api_call(
            lambda svc: (
                svc.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{tab_title}!A1",
                    valueInputOption="RAW",
                    body=body,
                )
            )
        )

    def append_rows_batch(
        self, tab_title: str, rows: list[list[Any]], headers: list[str] | None = None
    ) -> int:
        """
        Appends rows in chunks governed by `batch_size`.
        Uses `valueInputOption='RAW'` to prevent date corruption (e.g. '5/12') and formula execution.
        """
        if not rows:
            return 0

        is_new = self.ensure_sheet_exists(tab_title)
        if is_new and headers:
            self.ensure_sheet_headers(tab_title, headers)

        total_appended = 0
        for i in range(0, len(rows), self.batch_size):
            chunk = rows[i : i + self.batch_size]
            body = {"values": chunk}
            self._execute_api_call(
                lambda svc, b=body: (
                    svc.spreadsheets()
                    .values()
                    .append(
                        spreadsheetId=self.spreadsheet_id,
                        range=f"{tab_title}!A1",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body=b,
                    )
                )
            )
            total_appended += len(chunk)
            log.info("Appended chunk of %d rows to sheet tab '%s'", len(chunk), tab_title)

        return total_appended

    def sync_listings(
        self,
        tab_title: str,
        incoming_rows: list[list[Any]],
        headers: list[str] | None = None,
    ) -> tuple[int, int, int]:
        """
        Idempotent synchronization of listings with target Google Sheet tab.
        Inspects existing rows by Listing ID (Column A):
          - Appends newly discovered listings.
          - Updates modified listings in-place (via batchUpdate) when fields change.
          - Skips unchanged listings to conserve API quota and avoid table churn.

        Returns (inserted_count, updated_count, unchanged_count).
        """
        if not incoming_rows:
            return 0, 0, 0

        self.ensure_sheet_exists(tab_title)

        # Retrieve existing rows to check for duplicates and changes
        res = self._execute_api_call(
            lambda svc: (
                svc.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{tab_title}!A:L",
                )
            )
        )
        existing_values: list[list[Any]] = res.get("values", [])

        # If table is completely empty, initialize headers and append all
        if not existing_values:
            if headers:
                self.ensure_sheet_headers(tab_title, headers)
            self.append_rows_batch(tab_title=tab_title, rows=incoming_rows)
            log.info(
                "Sheet tab '%s' was empty: inserted %d new rows", tab_title, len(incoming_rows)
            )
            return len(incoming_rows), 0, 0

        # Build index of existing rows: listing_id -> (row_number, row_data)
        # Row 1 is header, data starts at Row 2
        existing_map: dict[str, tuple[int, list[Any]]] = {}
        for row_idx, r in enumerate(existing_values[1:], start=2):
            if r and r[0]:
                existing_map[str(r[0]).strip()] = (row_idx, r)

        to_update_data: list[dict[str, Any]] = []
        to_append: list[list[Any]] = []
        unchanged_count = 0

        for row in incoming_rows:
            lid = str(row[0]).strip()
            if lid in existing_map:
                row_idx, old_row = existing_map[lid]
                if row_data_changed(old_row, row):
                    to_update_data.append(
                        {
                            "range": f"{tab_title}!A{row_idx}:L{row_idx}",
                            "values": [row],
                        }
                    )
                else:
                    unchanged_count += 1
            else:
                to_append.append(row)

        # Execute batch updates in-place for modified listings
        if to_update_data:
            for i in range(0, len(to_update_data), self.batch_size):
                chunk = to_update_data[i : i + self.batch_size]
                body = {
                    "valueInputOption": "RAW",
                    "data": chunk,
                }
                self._execute_api_call(
                    lambda svc, b=body: (
                        svc.spreadsheets()
                        .values()
                        .batchUpdate(
                            spreadsheetId=self.spreadsheet_id,
                            body=b,
                        )
                    )
                )
            log.info("Updated %d modified rows in sheet tab '%s'", len(to_update_data), tab_title)

        # Batch append newly discovered listings
        if to_append:
            self.append_rows_batch(tab_title=tab_title, rows=to_append)

        inserted_count = len(to_append)
        updated_count = len(to_update_data)
        log.info(
            "Sync summary for '%s': %d inserted, %d updated, %d unchanged",
            tab_title,
            inserted_count,
            updated_count,
            unchanged_count,
        )
        return inserted_count, updated_count, unchanged_count
