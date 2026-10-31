# marketplace-crawler

[![CI](https://github.com/resilienter/marketplace-crawler/actions/workflows/ci.yml/badge.svg)](https://github.com/resilienter/marketplace-crawler/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small scraping pipeline for property listings. It fetches catalog pages with
several worker threads, parses and validates them, tracks price changes between
runs, and writes results to Google Sheets and Telegram.

This is a code sample: an independent rebuild, from scratch, of patterns from my
own commercial scraping work. It runs against bundled HTML fixtures, not real
sites, and contains no client data. Full commercial versions are not published.

## Run it

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
python -m src.main
```

No keys are needed. Without Google or Telegram credentials those steps are
logged as dry runs. `--simulate-anomaly` seeds higher historical prices so
price-drop alerts fire. `docker compose up --build` runs the same demo.

Output of `python -m src.main --simulate-anomaly`:

```text
2026-10-31 09:21:30 | INFO    | [main] Starting crawler (target: MetropolisRealty)
2026-10-31 09:21:30 | INFO    | [main] Simulate Anomaly mode active: resetting price history to pre-drop benchmark prices.
2026-10-31 09:21:30 | INFO    | [main] Dispatching 3 catalog pages across 3 worker threads...
2026-10-31 09:21:30 | INFO    | [main] [Worker] Crawling catalog page 1...
2026-10-31 09:21:30 | INFO    | [main] [Worker] Crawling catalog page 2...
2026-10-31 09:21:30 | INFO    | [main] [Worker] Crawling catalog page 3...
2026-10-31 09:21:31 | INFO    | [http_client] [CATALOG:P1] HTTP 200 in 0.01s -> https://example-realestate-marketplace.com/properties?page=1&sort=newest
2026-10-31 09:21:31 | INFO    | [http_client] [CATALOG:P2] HTTP 200 in 0.01s -> https://example-realestate-marketplace.com/properties?page=2&sort=newest
2026-10-31 09:21:31 | INFO    | [http_client] [CATALOG:P3] HTTP 200 in 0.01s -> https://example-realestate-marketplace.com/properties?page=3&sort=newest
2026-10-31 09:21:31 | INFO    | [main] [Worker] Page 1 finished: 3 valid listings parsed
2026-10-31 09:21:31 | INFO    | [telegram] [TELEGRAM DRY-RUN] Dispatch skipped (no credentials): >> [ALERT] <b>PRICE DROP ANOMALY DETECTED</b>
2026-10-31 09:21:31 | INFO    | [main] [Worker] Page 2 finished: 3 valid listings parsed
2026-10-31 09:21:31 | INFO    | [telegram] [TELEGRAM DRY-RUN] Dispatch skipped (no credentials): >> [ALERT] <b>PRICE DROP ANOMALY DETECTED</b>
2026-10-31 09:21:31 | INFO    | [main] [Worker] Page 3 finished: 2 valid listings parsed
2026-10-31 09:21:31 | INFO    | [telegram] [TELEGRAM DRY-RUN] Dispatch skipped (no credentials): >> [ALERT] <b>PRICE DROP ANOMALY DETECTED</b>
2026-10-31 09:21:31 | INFO    | [main] [GSHEETS DEMO] 8 unique records formatted for RAW batch export (Live sync bypassed: no credentials)
2026-10-31 09:21:31 | INFO    | [main] =========================================================================================================
2026-10-31 09:21:31 | INFO    | [main] PARSED & VALIDATED PROPERTY LISTINGS (Pydantic v2 Models):
2026-10-31 09:21:31 | INFO    | [main] ---------------------------------------------------------------------------------------------------------
2026-10-31 09:21:31 | INFO    | [main]   * [#101] Downtown Studio with Panoramic View        | 195,000.00 USD |  42.5 sqm | 4,588.24 USD/sqm | Fl 5/12
2026-10-31 09:21:31 | INFO    | [main]   * [#102] Spacious 2-Room Family Apartment           | 340,000.00 USD |  78.0 sqm | 4,358.97 USD/sqm | Fl 8/16
2026-10-31 09:21:31 | INFO    | [main]   * [#103] Penthouse Suite with Private Rooftop       | 520,000.00 USD | 135.5 sqm | 3,837.64 USD/sqm | Fl 12/12
2026-10-31 09:21:31 | INFO    | [main]   * [#104] Suburban Eco House with Private Garden     | 220,000.00 USD | 112.0 sqm | 1,964.29 USD/sqm | Fl 1/2
2026-10-31 09:21:31 | INFO    | [main]   * [#105] Old Town Renovated Duplex Loft             | 195,000.50 EUR |  85.4 sqm | 2,283.38 EUR/sqm | Fl 3/4
2026-10-31 09:21:31 | INFO    | [main]   * [#106] Cozy Furnished Studio near Metro Station   | 165,000.00 USD |  34.0 sqm | 4,852.94 USD/sqm | Fl 7/9
2026-10-31 09:21:31 | INFO    | [main]   * [#107] Victorian Terrace Apartment in West End    | 285,000.00 GBP |  65.0 sqm | 4,384.62 GBP/sqm | Fl 2/3
2026-10-31 09:21:31 | INFO    | [main]   * [#108] Urgent Sale: High-Floor Modern Condo       | 145,000.00 USD |  58.0 sqm | 2,500.00 USD/sqm | Fl 14/20
2026-10-31 09:21:31 | INFO    | [main] =========================================================================================================
2026-10-31 09:21:31 | INFO    | [main] Crawler finished in 0.03s. Total unique listings: 8 | Price anomaly alerts: 3
```

The HTTP 200 lines come from a fixture session that maps catalog URLs to the HTML files in `tests/fixtures/html/`; no network requests are made.

## What is inside

- `src/core/http_client.py`: GET with retries, shared 429 cooldown, HTML dumps on blocks, optional TLS impersonation via `curl_cffi`.
- `src/scrapers/marketplace.py`: XPath extraction, price parsing for US and EU formats.
- `src/models/schemas.py`: Pydantic models for listings and price signals.
- `src/services/gsheets.py`: upsert by listing ID, chunked writes.
- `src/services/telegram.py`: alerts, with the bot token removed from logged errors.

## Decisions

**One cooldown for all threads.** When any worker gets a 429, every worker waits. Backoff is `base × multiplier^(n−1)`, capped, with jitter; `Retry-After` is honoured up to cap.

**Upsert by listing ID.** Re-running updates changed rows and skips unchanged ones instead of appending duplicates.

**`RAW` writes to Sheets.** Prevents values like `5/12` from becoming dates and scraped text starting with `=` from running as formulas.

**Fixtures instead of live sites.** Reproducible demo without sending live network traffic.

## Limitations

- Selectors match the bundled demo markup; a real site needs its own.
- Price history is a JSON file in `data/`; there is no database.
- Threads, not asyncio: fine for dozens of pages, not for thousands.
- No proxy rotation or browser rendering.

## Tests

34 tests (`pytest -q`): retry and cooldown behaviour against a local server,
price parsing, schema validation, Sheets sync logic with a fake client.

## License

MIT. Educational and portfolio sample. Check robots.txt and site terms before scraping.
