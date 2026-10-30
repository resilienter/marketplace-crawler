import http.server
import socketserver
import threading
import time

import pytest

from src.config import settings
from src.core import http_client as hc

COUNTS: dict[str, int] = {}


class MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        n = COUNTS[self.path] = COUNTS.get(self.path, 0) + 1
        if self.path == "/rate" and n == 1:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b"slow down")
            return
        if self.path == "/flaky" and n == 1:
            self.send_response(503)
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/challenge" and n == 1:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>Just a moment...</html>")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>ok</html>")


@pytest.fixture(scope="module")
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), MockHTTPHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dumps_dir", tmp_path / "dumps")
    monkeypatch.setattr(settings, "hard_fail_on_antibot", False)
    monkeypatch.setattr(settings, "crawler_backoff_cap", 0.05)
    monkeypatch.setattr(settings, "crawler_jitter_max", 0.01)
    monkeypatch.setattr(settings, "crawler_connect_timeout", 1.0)
    monkeypatch.setattr(settings, "crawler_read_timeout", 1.0)
    COUNTS.clear()


@pytest.mark.parametrize("engine", ["requests", "curl_cffi"])
def test_retry_429_503_challenge_404(server, engine):
    session = hc.make_resilient_session(force_standard_requests=(engine == "requests"))
    coord = hc.CooldownCoordinator(0.05, 1.5, 0.2)
    assert hc.fetch_page(session, server + "/rate", coordinator=coord) == "<html>ok</html>"
    assert COUNTS["/rate"] == 2
    assert hc.fetch_page(session, server + "/flaky", coordinator=coord) == "<html>ok</html>"
    assert COUNTS["/flaky"] == 2
    assert hc.fetch_page(session, server + "/challenge", coordinator=coord) == "<html>ok</html>"
    with pytest.raises(hc.PageNotFound):
        hc.fetch_page(session, server + "/missing", coordinator=coord)
    assert COUNTS["/missing"] == 1  # not retried
    assert len(list(settings.dumps_dir.glob("blocked_429_*.html"))) == 1


@pytest.mark.parametrize("engine", ["requests", "curl_cffi"])
def test_network_error_is_retried_then_fetchfailed(engine):
    session = hc.make_resilient_session(force_standard_requests=(engine == "requests"))
    with pytest.raises(hc.FetchFailed):
        hc.fetch_page(
            session, "http://127.0.0.1:1/", coordinator=hc.CooldownCoordinator(0.01, 1.1, 0.05)
        )


def test_cooldown_is_shared_between_threads():
    coord = hc.CooldownCoordinator(0.3, 2.0, 2.0)
    ready = threading.Barrier(6)
    woke = []

    def worker():
        ready.wait()
        t0 = time.monotonic()
        coord.wait_if_needed()
        woke.append(time.monotonic() - t0)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    coord.register_trip(None)
    ready.wait()
    for t in ts:
        t.join()
    assert len(woke) == 5 and min(woke) >= 0.25


def test_retry_after_date_and_digits():
    assert hc.parse_retry_after("5") == 5.0
    assert hc.parse_retry_after(None) is None
    assert hc.parse_retry_after("garbage") is None
    assert hc.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_netscape_httponly_cookie(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        "example.com\tTRUE\t/\tFALSE\t0\tplain\tp1\n"
        "#HttpOnly_example.com\tTRUE\t/\tTRUE\t1893456000\tsession\ts1\n",
        encoding="utf-8",
    )
    jar = hc.load_cookies_from_file(f)
    assert {c.name for c in jar} == {"plain", "session"}
    assert [c for c in jar if c.name == "session"][0].secure is True
