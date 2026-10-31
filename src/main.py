import argparse
import concurrent.futures
import contextlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import BaseAdapter
from requests.models import Response

from src.config import settings
from src.core.http_client import cooldown
from src.core.logger import setup_logger
from src.models.schemas import PriceAnomalySignal, PropertyListing
from src.scrapers.marketplace import MarketplaceCrawler
from src.services.gsheets import GSHEETS_COLUMNS, GoogleSheetsWriter, listing_to_sheet_row
from src.services.telegram import TelegramNotifier

log = setup_logger("main", settings.logs_dir / "crawler.log")


class FixtureHttpAdapter(BaseAdapter):
    """
    Offline transport adapter that serves bundled local HTML fixtures.
    If a requested pagination page exceeds available fixtures, returns an empty
    DOM representing end-of-catalog pagination.
    """

    def __init__(self, fixtures_dir: Path):
        super().__init__()
        self.fixtures_dir = fixtures_dir

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> Response:
        resp = Response()
        resp.request = request
        resp.url = request.url

        page_num = 1
        if request.url and "page=" in request.url:
            with contextlib.suppress(Exception):
                page_num = int(request.url.split("page=")[1].split("&")[0])

        fixture_file = self.fixtures_dir / f"catalog_page_{page_num}.html"
        if fixture_file.exists():
            content = fixture_file.read_bytes()
        else:
            # Reached end of catalog pagination (empty results)
            content = b"""<!DOCTYPE html><html><body><main class="catalog"></main></body></html>"""

        resp.status_code = 200
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.raw = io.BytesIO(content)
        return resp

    def close(self) -> None:
        pass


def create_pipeline_session() -> requests.Session:
    """Creates a session pre-configured with local fixture routing for reproducible execution."""
    session = requests.Session()
    fixtures_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "html"
    adapter = FixtureHttpAdapter(fixtures_dir=fixtures_dir)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


DEFAULT_BENCHMARK_PRICES: dict[int, float] = {
    101: 215000.0,  # $195k vs $215k (-9.3% drop)
    104: 260000.0,  # $220k vs $260k (-15.4% drop)
    108: 185000.0,  # $145k vs $185k (-21.6% drop)
}


def load_price_history(history_file: Path, fallback_seed: Path | None = None) -> dict[int, float]:
    """Reads persistent price snapshot between pipeline executions."""
    if history_file.exists():
        try:
            data = json.loads(history_file.read_text(encoding="utf-8"))
            return {int(k): float(v) for k, v in data.items()}
        except Exception as err:
            log.warning("Could not read price history, falling back to seed fixture: %s", err)

    if fallback_seed and fallback_seed.exists():
        try:
            data = json.loads(fallback_seed.read_text(encoding="utf-8"))
            return {int(k): float(v) for k, v in data.items()}
        except Exception:
            pass

    return dict(DEFAULT_BENCHMARK_PRICES)


def save_price_history(history_file: Path, records: list[PropertyListing]) -> None:
    """Persists extracted prices to disk for next pipeline run comparison."""
    current_data: dict[int, float] = {}
    if history_file.exists():
        try:
            raw = json.loads(history_file.read_text(encoding="utf-8"))
            current_data = {int(k): float(v) for k, v in raw.items()}
        except Exception as err:
            log.warning(
                "Could not read existing price history, initializing fresh snapshot: %s", err
            )

    for r in records:
        current_data[r.listing_id] = r.price
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(current_data, indent=2), encoding="utf-8")


def crawl_page_worker(crawler: MarketplaceCrawler, page: int) -> tuple[int, list[PropertyListing]]:
    """Worker task executed concurrently across the thread pool."""
    cooldown.wait_if_needed()
    log.info("[Worker] Crawling catalog page %d...", page)
    html = crawler.fetch_listings_page(page)
    listings = crawler.parse_listings(html)
    if listings:
        log.info("[Worker] Page %d finished: %d valid listings parsed", page, len(listings))
    else:
        log.info("[Worker] Page %d: End of catalog reached (0 listings)", page)
    return page, listings


def run_pipeline(simulate_anomaly: bool = False, reset_history: bool = False) -> None:
    log.info("Starting crawler (target: %s)", settings.crawler_target_name)
    start_time = datetime.now(UTC)

    session = create_pipeline_session()
    crawler = MarketplaceCrawler(base_url=settings.crawler_base_url, session=session)
    telegram = TelegramNotifier()
    if telegram.enabled:
        log.info("Telegram alerting active (Target Chat ID: %s)", telegram.chat_id)
        telegram.send_message("<b>Marketplace Crawler Started</b>\nMonitoring catalog pages...")

    gsheets: GoogleSheetsWriter | None = None
    service_account_file = settings.resolved_service_account_path
    if settings.spreadsheet_id and service_account_file.exists():
        try:
            gsheets = GoogleSheetsWriter(
                spreadsheet_id=settings.spreadsheet_id,
                service_account_path=service_account_file,
                batch_size=settings.gsheets_batch_size,
            )
            log.info(
                "Initialized Google Sheets Writer for Spreadsheet ID: %s", settings.spreadsheet_id
            )
        except Exception as err:
            log.warning("Google Sheets initialization bypassed: %s", err)
    elif settings.spreadsheet_id and not service_account_file.exists():
        log.warning(
            "Google Sheets bypassed: credentials file not found at %s", service_account_file
        )

    history_file = settings.data_dir / "price_history.json"
    fallback_seed = (
        Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "data" / "seed_prices.json"
    )

    if reset_history and history_file.exists():
        log.info("Resetting price history file at %s", history_file)
        history_file.unlink()

    if simulate_anomaly:
        log.info(
            "Simulate Anomaly mode active: resetting price history to pre-drop benchmark prices."
        )
        if history_file.exists():
            history_file.unlink()
        price_history = load_price_history(history_file, fallback_seed=fallback_seed)
    else:
        price_history = load_price_history(history_file, fallback_seed=fallback_seed)

    # Unique entity storage indexed by listing_id to prevent duplicates across pagination
    collected_records: dict[int, PropertyListing] = {}
    signals: list[PriceAnomalySignal] = []

    pages_to_crawl = list(range(1, settings.crawler_max_pages + 1))
    log.info(
        "Dispatching %d catalog pages across %d worker threads...",
        len(pages_to_crawl),
        settings.crawler_concurrency,
    )

    # Real Concurrent Multi-threaded Pipeline Execution
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=settings.crawler_concurrency
    ) as executor:
        future_to_page = {executor.submit(crawl_page_worker, crawler, p): p for p in pages_to_crawl}
        for future in concurrent.futures.as_completed(future_to_page):
            page_num = future_to_page[future]
            try:
                _, listings = future.result()
                for item in listings:
                    if item.listing_id not in collected_records:
                        collected_records[item.listing_id] = item
                        hist_price = price_history.get(item.listing_id, 0.0)
                        signal = crawler.evaluate_price_drops(
                            item, historical_price=hist_price, threshold_pct=settings.sharp_drop_pct
                        )
                        if signal:
                            signals.append(signal)
                            telegram.notify_price_drop(signal)
            except Exception as err:
                log.error("Unhandled error on page %d: %s", page_num, err)

    unique_records = list(collected_records.values())

    # Persist updated prices for future anomaly evaluation
    save_price_history(history_file, unique_records)

    # Export to Google Sheets via Idempotent Smart Upsert
    if gsheets and unique_records:
        rows = [listing_to_sheet_row(item) for item in unique_records]
        ins, upd, unch = gsheets.sync_listings(
            tab_title="ActiveListings",
            incoming_rows=rows,
            headers=GSHEETS_COLUMNS,
        )
        log.info(
            "Google Sheets Sync: %d inserted, %d updated in-place, %d unchanged (idempotent)",
            ins,
            upd,
            unch,
        )
    else:
        log.info(
            "[GSHEETS DEMO] %d unique records formatted for RAW batch export (Live sync bypassed: no credentials)",
            len(unique_records),
        )

    # Print clean formatted data preview to console
    log.info("=" * 105)
    log.info("PARSED & VALIDATED PROPERTY LISTINGS (Pydantic v2 Models):")
    log.info("-" * 105)
    for item in sorted(unique_records, key=lambda x: x.listing_id):
        price_str = f"{item.price:10,.2f} {item.currency}"
        sqm_str = f"{item.price_per_sqm:8,.2f} {item.currency}/sqm"
        log.info(
            "  * [#%d] %-42s | %s | %5.1f sqm | %s | Fl %d/%d",
            item.listing_id,
            item.title[:42],
            price_str,
            item.area_sqm,
            sqm_str,
            item.floor,
            item.total_floors,
        )
    log.info("=" * 105)

    duration = (datetime.now(UTC) - start_time).total_seconds()
    log.info(
        "Crawler finished in %.2fs. Total unique listings: %d | Price anomaly alerts: %d",
        duration,
        len(unique_records),
        len(signals),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrapes property listings, tracks price drops, and syncs with Google Sheets."
    )
    parser.add_argument(
        "--simulate-anomaly",
        action="store_true",
        help="Seed higher historical prices to trigger price drop alerts.",
    )
    parser.add_argument(
        "--reset-history",
        action="store_true",
        help="Clear persistent price history from data directory.",
    )
    args = parser.parse_args()

    try:
        run_pipeline(
            simulate_anomaly=args.simulate_anomaly,
            reset_history=args.reset_history,
        )
    except KeyboardInterrupt:
        log.warning("Crawler interrupted by user.")
        sys.exit(0)
    except Exception as e:
        log.critical("Fatal unhandled exception in pipeline: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
