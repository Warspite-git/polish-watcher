"""Envio de alertas para o Telegram (Bot API)."""
from __future__ import annotations

import html
import json
import logging
import re
import time
from typing import Dict, List, Optional

import requests

from .fetcher import user_agent
from .models import (LOW_STOCK, NEW, PRICE, RESTOCK, STORE_CLOSED, STORE_ERROR, STORE_OPEN,
                     STORE_RECOVERED, VARIANT_RESTOCK, Alert)

API = "https://api.telegram.org"
logger = logging.getLogger("monitor")
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
ALBUM_SIZE = 10          # limite do Telegram por álbum
MAX_ALBUM_ITEMS = 50     # numa rodada gigante, o que passar disso vai só em texto
MIN_INTERVAL = 1.1       # segundos entre mensagens (limite ~1 msg/s por chat)

TITLES = {
    NEW: "🆕 <b>Produto novo</b>",
    RESTOCK: "✅ <b>De volta ao estoque</b>",
    VARIANT_RESTOCK: "✅ <b>Variante de volta ao estoque</b>",
    LOW_STOCK: "⏳ <b>Últimas unidades</b>",
    PRICE: "🏷️ <b>Preço mudou</b>",
    STORE_OPEN: "🔓 <b>Loja aberta</b>",
    STORE_CLOSED: "🔒 <b>Loja fechada com senha</b>",
    STORE_ERROR: "⚠️ <b>Não estou conseguindo ler a loja</b>",
    STORE_RECOVERED: "🔧 <b>Voltei a conseguir ler a loja</b>",
}
ICONS = {NEW: "🆕", RESTOCK: "✅", VARIANT_RESTOCK: "✅", LOW_STOCK: "⏳", PRICE: "🏷️"}
KIND_ORDER = {RESTOCK: 0, VARIANT_RESTOCK: 1, NEW: 2, LOW_STOCK: 3, PRICE: 4}


class TelegramError(Exception):
    pass


# ---------------------------------------------------------------- formatação

def brl(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "grátis"
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "_").replace(".", ",").replace("_", ".")


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def attr(text) -> str:
    return html.escape(str(text), quote=True)


def stock_text(a: Alert) -> str:
    if not a.available:
        return "esgotado"
    if a.stock is None:
        return "disponível"
    return "1 unidade" if a.stock == 1 else f"{a.stock} unidades"


def caption(a: Alert) -> str:
    lines = [f"{TITLES.get(a.kind, a.kind)} · {esc(a.store)}"]
    if a.is_store_alert:
        if a.note:
            lines.append(esc(a.note))
        lines.append(f'🔗 <a href="{attr(a.store_url)}">Abrir a loja</a>')
        return "\n".join(lines)
    lines.append(f"<b>{esc(_cut(a.name, 200))}</b>")
    if a.variants:
        lines.append("🎨 " + esc(_cut(", ".join(a.variants), 300)))
    if a.kind == PRICE and a.old_price is not None:
        lines.append(f"💰 <s>{brl(a.old_price)}</s> → <b>{brl(a.price)}</b>")
    elif a.price is not None:
        lines.append(f"💰 {brl(a.price)}")
    lines.append("📦 " + stock_text(a))
    if a.note:
        lines.append(esc(a.note))
    lines.append(f'🔗 <a href="{attr(a.url)}">Ver na loja</a>')
    return "\n".join(lines)


def compact_line(a: Alert, index: Optional[int] = None) -> str:
    """Uma linha por alerta, para álbuns e resumos."""
    prefix = f"{index}. " if index is not None else ""
    price = (f"<s>{brl(a.old_price)}</s> → {brl(a.price)}" if a.kind == PRICE and a.old_price is not None
             else brl(a.price))
    stock = "" if a.stock is None or not a.available else f" · {a.stock} un."
    return f'{prefix}{ICONS.get(a.kind, "•")} <a href="{attr(a.url)}">{esc(_cut(a.name, 45))}</a> — {price}{stock}'


def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _cut(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _summary_header(store: str, count: int, part: str = "") -> str:
    noun = "alerta" if count == 1 else "alertas"
    return f"📣 <b>{esc(store)}</b> · {count} {noun}{part}"


# ---------------------------------------------------------------- cliente

class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: float = 30):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self._last = 0.0

    def redact(self, text: str) -> str:
        """Remove o token de qualquer texto (a URL da API contém o token)."""
        return text.replace(self.token, "***") if self.token else text

    def call(self, method: str, data: Optional[dict] = None, files: Optional[dict] = None, retries: int = 3):
        url = f"{API}/bot{self.token}/{method}"
        last = "erro desconhecido"
        for attempt in range(retries + 1):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.post(url, data=data, files=files, timeout=self.timeout)
            except Exception as e:  # qualquer erro pode conter a URL (com o token)
                last = self.redact(f"{type(e).__name__}: {e}")
                self._last = time.monotonic()
                time.sleep(2 * (attempt + 1))
                continue
            self._last = time.monotonic()
            try:
                body = r.json()
            except ValueError:
                body = {"ok": False, "description": f"HTTP {r.status_code}"}
            if body.get("ok"):
                return body.get("result")
            last = self.redact(f"{method}: {body.get('description')}")
            new_chat = (body.get("parameters") or {}).get("migrate_to_chat_id")
            if new_chat and data and str(data.get("chat_id")) == self.chat_id:
                # O grupo virou supergrupo e ganhou outro chat_id: segue o novo.
                logger.warning(f"o grupo do Telegram mudou de ID; atualize o TELEGRAM_CHAT_ID para {new_chat}")
                self.chat_id = str(new_chat)
                data = {**data, "chat_id": self.chat_id}
                continue
            if r.status_code == 429:
                time.sleep(float((body.get("parameters") or {}).get("retry_after", 5)) + 1)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise TelegramError(last)
        raise TelegramError(last)

    # --- mensagens básicas

    def send_text(self, text: str, preview: bool = False):
        return self.call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": not preview}),
        })

    def send_photo(self, photo_url: str, text: str):
        data = {"chat_id": self.chat_id, "caption": text, "parse_mode": "HTML"}
        try:
            return self.call("sendPhoto", {**data, "photo": photo_url})
        except TelegramError:
            pass
        # O Telegram não conseguiu baixar a imagem: baixamos nós e enviamos o arquivo.
        content = _download(photo_url)
        if content is None:
            raise TelegramError("não foi possível baixar a imagem")
        return self.call("sendPhoto", data, files={"photo": ("produto.jpg", content)})

    def send_album(self, alerts: List[Alert], header: str):
        lines = [header]
        for i, a in enumerate(alerts, start=1):
            lines.append(compact_line(a, i))
        text = "\n".join(lines)
        while visible_len(text) > CAPTION_LIMIT and len(lines) > 2:
            lines.pop()
            text = "\n".join(lines + ["…"])
        media = [{"type": "photo", "media": a.image} for a in alerts]
        media[0].update({"caption": text, "parse_mode": "HTML"})
        return self.call("sendMediaGroup", {"chat_id": self.chat_id, "media": json.dumps(media)})

    def send_digest(self, alerts: List[Alert]):
        """Resumo só em texto, dividido em mensagens dentro do limite do Telegram."""
        by_store: Dict[str, List[Alert]] = {}
        for a in alerts:
            by_store.setdefault(a.store, []).append(a)
        chunks, current = [], ""
        for store, items in by_store.items():
            block = [_summary_header(store, len(items))] + [compact_line(a) for a in items]
            for line in block:
                if current and len(current) + len(line) + 1 > MESSAGE_LIMIT - 100:
                    chunks.append(current)
                    current = ""
                current = f"{current}\n{line}" if current else line
            current += "\n"
        if current.strip():
            chunks.append(current)
        for chunk in chunks:
            self.send_text(chunk.strip())

    # --- alertas

    def send_alert(self, a: Alert):
        text = caption(a)
        if a.image and not a.is_store_alert:
            try:
                return self.send_photo(a.image, text)
            except TelegramError:
                pass  # sem foto: manda texto com prévia do link (o Telegram mostra a imagem da loja)
        return self.send_text(text, preview=not a.is_store_alert)

    def send_alerts(self, alerts: List[Alert], max_individual: int = 10) -> List[Alert]:
        """Envia tudo e devolve os alertas que falharam (para tentar de novo na próxima rodada)."""
        failed: List[Alert] = []
        store_alerts = [a for a in alerts if a.is_store_alert]
        product_alerts = [a for a in alerts if not a.is_store_alert]

        for a in store_alerts:
            try:
                self.send_alert(a)
            except TelegramError:
                failed.append(a)

        if len(product_alerts) <= max_individual:
            for a in product_alerts:
                try:
                    self.send_alert(a)
                except TelegramError:
                    failed.append(a)
            return failed

        # Muitos alertas: álbuns de até 10 fotos por loja, com a lista numerada na legenda.
        by_store: Dict[str, List[Alert]] = {}
        for a in product_alerts:
            by_store.setdefault(a.store, []).append(a)
        budget = MAX_ALBUM_ITEMS
        leftovers: List[Alert] = []
        for store, items in by_store.items():
            items.sort(key=lambda a: KIND_ORDER.get(a.kind, 9))
            with_image = [a for a in items if a.image]
            take = with_image[:budget]
            budget -= len(take)
            leftovers += with_image[len(take):] + [a for a in items if not a.image]
            chunks = [take[i:i + ALBUM_SIZE] for i in range(0, len(take), ALBUM_SIZE)]
            for n, chunk in enumerate(chunks, start=1):
                part = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
                try:
                    if len(chunk) == 1:
                        self.send_alert(chunk[0])
                    else:
                        self.send_album(chunk, _summary_header(store, len(items), part))
                except TelegramError:
                    leftovers += chunk
        if leftovers:
            try:
                self.send_digest(leftovers)
            except TelegramError:
                failed += leftovers
        return failed

    def get_updates(self) -> list:
        return self.call("getUpdates", {"timeout": 0}) or []


def _download(url: str, limit: int = 10 * 1024 * 1024) -> Optional[bytes]:
    """Baixa a foto (só HTTPS, com limite de tamanho) para reenviar como arquivo."""
    if not url.startswith("https://"):
        return None
    try:
        with requests.get(url, headers={"User-Agent": user_agent()}, timeout=30, stream=True) as r:
            if not r.ok:
                return None
            content = b""
            for chunk in r.iter_content(64 * 1024):
                content += chunk
                if len(content) > limit:
                    return None
            return content or None
    except requests.RequestException:
        return None
