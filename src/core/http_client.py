"""HTTP engine: shared 429 cooldown, forensic dumps, optional TLS-impersonation backend."""

from __future__ import annotations

import random
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from src.config import settings
from src.core.logger import setup_logger

try:  # optional extra: pip install ".[stealth]"
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests import exceptions as cffi_exceptions
except ImportError:
    cffi_requests = None
    cffi_exceptions = None

log = setup_logger("http_client", settings.logs_dir / "crawler.log")

# curl_cffi raises its own exception hierarchy, unrelated to requests.exceptions,
# so both families have to be listed or retries silently stop working on the main path.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)
if cffi_exceptions is not None:
    NETWORK_ERRORS += (cffi_exceptions.ConnectionError, cffi_exceptions.Timeout)

# Only for plain requests sessions. With TLS impersonation the profile ships its own
# consistent header set; overriding User-Agent would contradict the TLS fingerprint.
HEADERS_POOL: list[dict[str, str]] = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    },
]

# Kept narrow on purpose: broad words ("access denied", "turnstile") match normal pages.
ANTI_BOT_MARKERS: tuple[str, ...] = (
    "cf-browser-verification",
    "challenge-running",
    "please confirm that you are human",
    "just a moment...",
)
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class ScrapingError(Exception):
    """Base class for crawler errors."""


class BlockedError(ScrapingError):
    reason = "blocked"

    def __init__(self, label: str, url: str, dump_path: Path | None = None) -> None:
        super().__init__(f"[{label}] {self.reason}: {url} (dump={dump_path})")
        self.label = label
        self.url = url
        self.dump_path = dump_path


class RateLimit429(BlockedError):
    reason = "HTTP 429 rate limit"


class Forbidden403(BlockedError):
    reason = "HTTP 403 forbidden"


class CaptchaError(BlockedError):
    reason = "anti-bot challenge"


class PageNotFound(ScrapingError):
    """404/410: retrying will not help."""


class FetchFailed(ScrapingError):
    """All attempts exhausted."""


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class CooldownCoordinator:
    """One pause shared by every worker thread after an HTTP 429."""

    def __init__(
        self, base_s: float, multiplier: float, cap_s: float, enabled: bool = True
    ) -> None:
        self.enabled = enabled
        self._base_s = base_s
        self._multiplier = multiplier
        self._cap_s = cap_s
        self._lock = threading.Lock()
        self._blocked_until = 0.0  # time.monotonic() value
        self._trips = 0

    def register_trip(self, retry_after: float | None) -> float:
        """Record a 429 and return how long everybody should wait."""
        with self._lock:
            self._trips += 1
            wait_s = min(self._cap_s, self._base_s * self._multiplier ** (self._trips - 1))
            if retry_after is not None:
                wait_s = max(wait_s, min(self._cap_s, retry_after))
            wait_s += random.uniform(0.0, max(0.1, wait_s * 0.1))
            if self.enabled:
                self._blocked_until = max(self._blocked_until, time.monotonic() + wait_s)
            return wait_s

    def register_success(self) -> None:
        with self._lock:
            if time.monotonic() >= self._blocked_until:
                self._trips = 0

    def wait_if_needed(self) -> float:
        """Sleep outside the lock; small jitter so waiting threads do not wake in lockstep."""
        if not self.enabled:
            return 0.0
        with self._lock:
            remaining = self._blocked_until - time.monotonic()
        if remaining <= 0:
            return 0.0
        log.warning("Global 429 cooldown active: sleeping %.1fs", remaining)
        time.sleep(remaining + random.uniform(0.0, 0.5))
        return remaining


cooldown = CooldownCoordinator(
    base_s=settings.global_429_cooldown_base_s,
    multiplier=settings.global_429_cooldown_multiplier,
    cap_s=settings.global_429_cooldown_cap_s,
    enabled=settings.enable_global_429_cooldown,
)


def make_resilient_session(pool_size: int = 20, force_standard_requests: bool = False) -> Any:
    """curl_cffi session with browser TLS impersonation if available, else plain requests."""
    if settings.use_curl_cffi and cffi_requests is not None and not force_standard_requests:
        try:
            session = cffi_requests.Session(impersonate=settings.curl_impersonate)
            log.info("curl_cffi session, impersonate=%s", settings.curl_impersonate)
            return session
        except Exception as exc:  # unknown profile name or missing native lib
            log.warning("curl_cffi init failed (%s); falling back to requests", exc)

    session = requests.Session()
    session.headers.update(HEADERS_POOL[0])
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    log.info("requests session with connection pooling")
    return session


def load_cookies_from_file(path: Path | str) -> requests.cookies.RequestsCookieJar:
    """Netscape cookies.txt (incl. '#HttpOnly_' lines), 'a=b; c=d' header string, or key=value lines."""
    jar = requests.cookies.RequestsCookieJar()
    file_path = Path(path)
    if not file_path.exists():
        log.warning("Cookie file not found: %s", file_path)
        return jar

    content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]

    netscape_rows = []
    for line in lines:
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        if line.count("\t") >= 6:
            netscape_rows.append(line.split("\t"))

    if netscape_rows:
        for domain, _, c_path, secure, expires, name, value in (r[:7] for r in netscape_rows):
            jar.set(
                name,
                value,
                domain=domain,
                path=c_path,
                secure=secure.upper() == "TRUE",
                expires=int(expires) if expires.isdigit() and int(expires) > 0 else None,
            )
        return jar

    if len(lines) == 1 and ";" in lines[0] and "=" in lines[0]:
        for pair in lines[0].split(";"):
            if "=" in pair:
                key, val = pair.split("=", 1)
                jar.set(key.strip(), val.strip())
        return jar

    for line in lines:
        if not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            jar.set(key.strip(), val.strip())
    return jar


def apply_cookies(session: Any, jar: requests.cookies.RequestsCookieJar) -> None:
    """Works for both engines; curl_cffi sessions do not accept a requests jar via update()."""
    for cookie in jar:
        session.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)


def is_antibot_payload(html: str) -> bool:
    head = (html or "")[:20_000].lower()
    return any(marker in head for marker in ANTI_BOT_MARKERS)


def _dump(kind: str, url: str, body: str) -> Path:
    settings.dumps_dir.mkdir(parents=True, exist_ok=True)
    host = urlparse(url).netloc.replace(":", "_") or "unknown"
    path = settings.dumps_dir / f"{kind}_{host}_{time.time_ns()}.html"
    path.write_text(body or "", encoding="utf-8", errors="ignore")
    return path


def _backoff_seconds(attempt: int) -> float:
    return min(settings.crawler_backoff_cap, 2.0**attempt) + random.uniform(
        0.0, settings.crawler_jitter_max
    )


def fetch_page(
    session: Any,
    url: str,
    label: str = "GET",
    *,
    raise_on_block: bool = False,
    coordinator: CooldownCoordinator | None = None,
) -> str:
    """GET with shared 429 cooldown, 5xx/network retries and forensic dumps.

    raise_on_block=True raises the first 403/429/challenge instead of retrying.
    """
    pause = coordinator or cooldown
    last_exc: BaseException | None = None

    for attempt in range(1, settings.crawler_max_attempts + 1):
        pause.wait_if_needed()
        started = time.monotonic()
        try:
            resp = session.get(
                url, timeout=(settings.crawler_connect_timeout, settings.crawler_read_timeout)
            )
        except NETWORK_ERRORS as exc:
            last_exc = exc
            wait_s = _backoff_seconds(attempt)
            log.warning(
                "[%s] network error attempt=%d: %r, sleeping %.1fs", label, attempt, exc, wait_s
            )
            time.sleep(wait_s)
            continue

        status = resp.status_code
        log.info("[%s] HTTP %d in %.2fs -> %s", label, status, time.monotonic() - started, url)

        if status == 429:
            dump = _dump("blocked_429", url, resp.text)
            wait_s = pause.register_trip(parse_retry_after(resp.headers.get("Retry-After")))
            log.warning("[%s] 429: cooldown %.1fs, dump=%s", label, wait_s, dump)
            last_exc = RateLimit429(label, url, dump)
            if raise_on_block:
                raise last_exc
            if not pause.enabled:
                time.sleep(wait_s)
            continue

        if status == 403:
            dump = _dump("blocked_403", url, resp.text)
            last_exc = Forbidden403(label, url, dump)
            if raise_on_block or settings.hard_fail_on_403:
                raise last_exc
            time.sleep(_backoff_seconds(attempt))
            continue

        if status in RETRYABLE_STATUS:
            last_exc = ScrapingError(f"[{label}] upstream HTTP {status}: {url}")
            time.sleep(_backoff_seconds(attempt))
            continue

        if status in (404, 410):
            raise PageNotFound(f"[{label}] HTTP {status}: {url}")
        if status >= 400:
            raise ScrapingError(f"[{label}] HTTP {status}: {url}")

        if is_antibot_payload(resp.text):
            dump = _dump("challenge", url, resp.text)
            if isinstance(session, requests.Session):  # header rotation only makes sense here
                session.headers.update(random.choice(HEADERS_POOL))
            last_exc = CaptchaError(label, url, dump)
            if raise_on_block or settings.hard_fail_on_antibot:
                raise last_exc
            time.sleep(_backoff_seconds(attempt))
            continue

        pause.register_success()
        return resp.text

    raise FetchFailed(
        f"[{label}] failed after {settings.crawler_max_attempts} attempts: {url}"
    ) from last_exc
