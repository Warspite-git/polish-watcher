"""Proteções contra conteúdo malicioso vindo das lojas."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from monitor import fetcher as fetcher_mod
from monitor.adapters.nuvemshop import parse_listing
from monitor.adapters.shopify import parse_product
from monitor.fetcher import Fetcher, FetchError
from monitor.models import clean_text
from monitor.runner import StoreLog
from tests.test_nuvemshop import item


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/fora":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/dentro":
            self.send_response(301)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/grande":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 300_000)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_redirect_inside_store_is_followed(server):
    r = Fetcher(delay=0, allowed_host="127.0.0.1", retries=0).get(server + "/dentro")
    assert r.text == "ok" and r.url.endswith("/ok")


def test_redirect_to_other_host_is_refused(server):
    with pytest.raises(FetchError, match="fora da loja"):
        Fetcher(delay=0, allowed_host="127.0.0.1", retries=0).get(server + "/fora")


def test_url_outside_store_is_refused():
    with pytest.raises(FetchError, match="fora da loja"):
        Fetcher(delay=0, allowed_host="loja.test", retries=0).get("https://outro.test/produtos/x/")
    with pytest.raises(FetchError, match="esquema"):
        Fetcher(delay=0, retries=0).get("file:///etc/passwd")


def test_response_size_limit(server, monkeypatch):
    monkeypatch.setattr(fetcher_mod, "MAX_BYTES", 100_000)
    monkeypatch.setattr(fetcher_mod._read_limited, "__defaults__", (100_000, 120))
    with pytest.raises(FetchError, match="passou de"):
        Fetcher(delay=0, allowed_host="127.0.0.1", retries=0).get(server + "/grande")


def test_clean_text_blocks_workflow_command_injection():
    evil = "Esmalte\n::add-mask::segredo\r\n::error::falso\x1b[31m"
    cleaned = clean_text(evil)
    assert "\n" not in cleaned and "\r" not in cleaned and "\x1b" not in cleaned
    assert not cleaned.startswith("::")


def test_store_log_is_single_line(caplog):
    from monitor.config import StoreConfig
    log = StoreLog(__import__("logging").getLogger("t"), {"store": StoreConfig(name="L", url="https://l.test", key="l.test")})
    with caplog.at_level("INFO"):
        log.info("produto 'A\n::warning::B'")
    assert "\n" not in caplog.records[0].getMessage()


def test_listing_sanitizes_names_and_rejects_bad_urls():
    html = (item("1", "3", '{"name": "Bonito\\n::error::x", "offers": {"url": "https://loja.test/p/1", '
                           '"price": "1", "availability": "InStock"}}')
            + item("2", "3", '{"name": "Mau", "offers": {"url": "javascript:alert(1)", "price": "1"}}'))
    products, errors = parse_listing(html)
    assert [p.name for p in products] == ["Bonito ::error::x"]
    assert errors == 1


def test_shopify_handle_is_url_encoded():
    p = parse_product({"id": 1, "title": "X", "handle": "a/../../b?c", "variants": [], "images": [
        {"src": "http://inseguro.test/img.jpg"}]}, "https://loja.test")
    assert p.url == "https://loja.test/products/a%2F..%2F..%2Fb%3Fc"
    assert p.image is None  # imagem sem HTTPS é descartada
