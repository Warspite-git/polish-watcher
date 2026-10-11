"""HTTP educado e defensivo.

- User-Agent identificável, pausa entre requisições, timeout e retry com backoff.
- Cada loja só acessa o próprio domínio: redirecionamentos para outros sites são
  recusados (o HTML das lojas não consegue fazer o monitor acessar outros endereços).
- Respostas têm limite de tamanho e de tempo total (proteção contra respostas
  gigantes ou "bombas" de compressão).
"""
from __future__ import annotations

import os
import random
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BYTES = 20 * 1024 * 1024   # maior página observada nas lojas: ~5 MB
MAX_SECONDS = 120              # tempo máximo para baixar uma resposta
MAX_REDIRECTS = 5


def user_agent() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    contact = f"+https://github.com/{repo}" if repo else "monitor pessoal de estoque"
    return f"PolishWatcher/1.0 ({contact})"


def host_key(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, delay: float = 1.5, allowed_host: Optional[str] = None, timeout=(10, 40),
                 retries: int = 3, backoff: float = 2.0):
        self.delay = delay
        self.allowed_host = allowed_host  # ex.: "lojawhatcha.com.br" (sem www)
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.requests = 0
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent(),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        })

    def _check_url(self, url: str):
        scheme = urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise FetchError(f"esquema não permitido: {url[:80]}")
        if self.allowed_host and host_key(url) != self.allowed_host:
            raise FetchError(f"endereço fora da loja recusado: {url[:80]}")

    def _wait_turn(self):
        elapsed = time.monotonic() - self._last
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _get_once(self, url: str) -> requests.Response:
        """Uma requisição, seguindo redirecionamentos só dentro do domínio permitido."""
        for _ in range(MAX_REDIRECTS + 1):
            self._check_url(url)
            resp = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=False)
            if resp.is_redirect:
                location = resp.headers.get("Location", "")
                resp.close()
                url = urljoin(url, location)
                continue
            return resp
        raise FetchError(f"redirecionamentos demais em {url[:80]}")

    def get(self, url: str) -> requests.Response:
        last_error = "erro desconhecido"
        for attempt in range(self.retries + 1):
            self._wait_turn()
            retry_after = None
            try:
                resp = self._get_once(url)
                self.requests += 1
                if resp.status_code in RETRY_STATUS:
                    last_error = f"HTTP {resp.status_code}"
                    retry_after = _retry_after(resp)
                    resp.close()
                elif resp.status_code >= 400:
                    resp.close()
                    raise FetchError(f"HTTP {resp.status_code} em {url}")
                else:
                    return _read_limited(resp)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
            finally:
                self._last = time.monotonic()
            if attempt < self.retries:
                wait = retry_after if retry_after is not None else self.backoff * (2 ** attempt)
                time.sleep(min(wait, 60) + random.uniform(0, 0.5))
        raise FetchError(f"{last_error} em {url} (após {self.retries + 1} tentativas)")


def _read_limited(resp: requests.Response, max_bytes: int = MAX_BYTES,
                  max_seconds: float = MAX_SECONDS) -> requests.Response:
    """Lê o corpo (já descomprimido) em pedaços, abortando se passar dos limites."""
    declared = resp.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        resp.close()
        raise FetchError(f"resposta grande demais ({declared} bytes) em {resp.url}")
    start = time.monotonic()
    chunks, total = [], 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise FetchError(f"resposta passou de {max_bytes // 1024 // 1024} MB em {resp.url}")
            if time.monotonic() - start > max_seconds:
                raise FetchError(f"resposta demorou mais de {max_seconds}s em {resp.url}")
            chunks.append(chunk)
    finally:
        resp.close()
    resp._content = b"".join(chunks)  # a partir daqui .text e .json() funcionam normalmente
    resp._content_consumed = True
    return resp


def _retry_after(resp: requests.Response):
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def is_password_page(url: str) -> bool:
    """Nuvemshop e Shopify redirecionam para /password quando a loja está fechada."""
    path = urlparse(url).path.rstrip("/")
    return path.endswith("/password")
