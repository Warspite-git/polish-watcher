"""Envio de alertas para o Telegram (Bot API)."""
from __future__ import annotations

import html
import io
import json
import logging
import re
import time
import unicodedata
from typing import Dict, List, Optional

import requests

from .fetcher import user_agent
from .models import (LOW_STOCK, NEW, PRICE, REPLENISH, RESTOCK, RETRACT, SOLD_OUT, STORE_CLOSED, STORE_ERROR, STORE_OPEN,
                     STORE_RECOVERED, VARIANT_RESTOCK, Alert, clean_text)

API = "https://api.telegram.org"
logger = logging.getLogger("monitor")
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
ALBUM_SIZE = 10          # limite do Telegram por álbum
MAX_ALBUM_ITEMS = 50     # numa rodada gigante, o que passar disso vai só em texto
MIN_INTERVAL = 1.1       # segundos entre mensagens (limite ~1 msg/s por chat)

TITLES = {
    NEW: "🆕 <b>NOVIDADE</b>",
    RESTOCK: "🚨 <b>RESTOCK</b>",
    VARIANT_RESTOCK: "🚨 <b>RESTOCK DE VARIANTE</b>",
    REPLENISH: "📦 <b>REPOSIÇÃO</b>",
    LOW_STOCK: "⏳ <b>ÚLTIMAS UNIDADES</b>",
    PRICE: "🏷️ <b>PREÇO MUDOU</b>",
    SOLD_OUT: "⚡ <b>ESGOTOU</b>",
    RETRACT: "🗑️ <b>ALERTA APAGADO (produto retirado do site)</b>",
    STORE_OPEN: "🔓 <b>LOJA ABERTA</b>",
    STORE_CLOSED: "🔒 <b>LOJA FECHADA COM SENHA</b>",
    STORE_ERROR: "⚠️ <b>NÃO ESTOU CONSEGUINDO LER A LOJA</b>",
    STORE_RECOVERED: "🔧 <b>VOLTEI A LER A LOJA</b>",
}
ICONS = {NEW: "🆕", RESTOCK: "🚨", VARIANT_RESTOCK: "🚨", REPLENISH: "📦", LOW_STOCK: "⏳", PRICE: "🏷️",
         SOLD_OUT: "⚡"}
# Hashtags por tipo: tocando nelas, o membro filtra o histórico do grupo
TYPE_TAGS = {
    NEW: "#novidade", RESTOCK: "#restock", VARIANT_RESTOCK: "#restock", REPLENISH: "#reposicao",
    LOW_STOCK: "#ultimasunidades",
    PRICE: "#preco", SOLD_OUT: "#esgotou", STORE_OPEN: "#lojaaberta", STORE_CLOSED: "#lojafechada",
    STORE_ERROR: "#aviso", STORE_RECOVERED: "#aviso",
}
# Avisos menos urgentes chegam sem som. "Loja aberta", novidades e restocks tocam.
SILENT = {PRICE, SOLD_OUT, STORE_CLOSED, STORE_ERROR, STORE_RECOVERED}
URGENT_KINDS = {NEW, RESTOCK, VARIANT_RESTOCK, REPLENISH, LOW_STOCK}
KIND_ORDER = {RESTOCK: 0, VARIANT_RESTOCK: 1, NEW: 2, REPLENISH: 3, LOW_STOCK: 4, PRICE: 5}


class TelegramError(Exception):
    pass


def _who(a: Alert) -> str:
    """Identifica o alerta nos logs (públicos): loja privada não tem nome nem produto revelados."""
    return "loja privada" if a.private else f"{clean_text(a.store)}: {clean_text(a.name)[:50]}"


# ---------------------------------------------------------------- formatação

def brl(value: Optional[float]) -> str:
    if value is None:
        return "—"
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "_").replace(".", ",").replace("_", ".")


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def attr(text) -> str:
    return html.escape(str(text), quote=True)


def hashtag(text: str) -> str:
    """"By Ana Lima" → "#ByAnaLima" (sem acentos, espaços ou pontuação)."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    tag = "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", text))
    if not tag:
        return ""
    return "#" + ("Loja" + tag if tag[0].isdigit() else tag)


def tags(a: Alert) -> str:
    store = [hashtag(t) for t in a.store_tag.split()] if a.store_tag else [hashtag(a.store)]
    extra = ["#brinde"] if is_freebie(a) else []
    return " ".join(t for t in [*store, TYPE_TAGS.get(a.kind, ""), *extra] if t)


def is_freebie(a: Alert) -> bool:
    """Preço zero costuma ser brinde de compra acima de um valor, não produto grátis."""
    return not a.is_store_alert and a.price == 0


def price_line(a: Alert) -> str:
    if a.price is None:
        return ""
    if is_freebie(a):
        return "🎁 R$ 0,00 (possível brinde)"
    return f"💰 {brl(a.price)}"


def is_urgent(a: Alert, urgent_stock: int) -> bool:
    return (a.kind in URGENT_KINDS and a.available and isinstance(a.stock, int)
            and 0 < a.stock <= urgent_stock)


def stock_text(a: Alert, show_stock: bool = True) -> str:
    if not a.available:
        return "esgotado"
    if a.stock is None or not show_stock:
        return "disponível"
    return "1 unidade" if a.stock == 1 else f"{a.stock} unidades"


def caption(a: Alert, urgent_stock: int = 5, with_link: bool = False, show_stock: bool = True) -> str:
    """Legenda do alerta. Os links vão em botões; with_link=True os coloca no texto."""
    if a.kind == SOLD_OUT:
        return "\n".join([f"⚡ <b>{esc(a.note or 'Esgotou')}!</b>",
                          f"<b>{esc(_cut(a.name, 200))}</b> · {esc(a.store)}", tags(a)])
    lines = [f"{TITLES.get(a.kind, a.kind)} · {esc(a.store)}"]
    if a.is_store_alert:
        if a.note:
            lines.append(esc(a.note))
        if with_link:
            lines.append(f'🔗 <a href="{attr(a.store_url)}">Abrir a loja</a>')
        lines.append(tags(a))
        return "\n".join(lines)
    lines.append(f"<b>{esc(_cut(a.name, 200))}</b>")
    if a.variants:
        lines.append("🎨 " + esc(_cut(", ".join(a.variants), 300)))
    if a.kind == PRICE and a.old_price is not None:
        price = f"💰 <s>{brl(a.old_price)}</s> → <b>{brl(a.price)}</b>"
    else:
        price = price_line(a)
    if a.kind == REPLENISH and a.old_stock is not None and isinstance(a.stock, int):
        change = (f"📦 {a.old_stock} → {a.stock} unidades (+{a.stock - a.old_stock})" if show_stock
                  else "📦 Chegaram mais unidades")
        lines.append(" · ".join(x for x in (price, change) if x))
    elif is_urgent(a, urgent_stock):
        lines.append(price)
        if not show_stock:
            lines.append("🔥 Últimas unidades. Corre!")
        else:
            lines.append("🔥 Última unidade. Corre!" if a.stock == 1 else f"🔥 Só {a.stock} unidades. Corre!")
    else:
        lines.append(" · ".join(x for x in (price, "📦 " + stock_text(a, show_stock)) if x))
    if a.note:
        lines.append(esc(a.note))
    if with_link:
        lines.append(f'🔗 <a href="{attr(a.url)}">Ver na loja</a>')
    lines.append(tags(a))
    return "\n".join(lines)


def buttons(a: Alert, urgent_stock: int = 5) -> Optional[dict]:
    """Botões de toque abaixo do alerta (só links http/https)."""
    if a.kind == SOLD_OUT:
        return None
    row = []
    if a.is_store_alert:
        row.append({"text": "🏪 Abrir a loja", "url": a.store_url})
    else:
        label = "🛒 Comprar agora" if is_urgent(a, urgent_stock) else ("🛒 Comprar" if a.available else "🔎 Ver produto")
        row.append({"text": label, "url": a.url})
        row.append({"text": "🏪 Ver a loja", "url": a.store_url})
    row = [b for b in row if str(b["url"]).startswith(("https://", "http://"))]
    return {"inline_keyboard": [row]} if row else None


def compact_line(a: Alert, index: Optional[int] = None, urgent_stock: int = 5, show_stock: bool = True) -> str:
    """Uma linha por alerta, para álbuns e resumos."""
    prefix = f"{index}. " if index is not None else ""
    price = (f"<s>{brl(a.old_price)}</s> → {brl(a.price)}" if a.kind == PRICE and a.old_price is not None
             else ("🎁 possível brinde" if is_freebie(a) else brl(a.price)))
    stock = ""
    if a.available and isinstance(a.stock, int):
        if not show_stock:
            stock = " · 🔥 últimas unidades" if is_urgent(a, urgent_stock) else ""
        else:
            stock = f" · 🔥 {a.stock} un." if is_urgent(a, urgent_stock) else f" · {a.stock} un."
    elif not a.available:
        stock = " · esgotado"
    return f'{prefix}{ICONS.get(a.kind, "•")} <a href="{attr(a.url)}">{esc(_cut(a.name, 45))}</a> — {price}{stock}'


def group_tags(alerts: List[Alert]) -> str:
    out: List[str] = []
    for a in alerts:
        for t in tags(a).split():
            if t not in out:
                out.append(t)
    return " ".join(out)


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
    def __init__(self, token: str, chat_id: str, timeout: float = 30, urgent_stock: int = 5,
                 show_stock: bool = True):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.urgent_stock = urgent_stock
        self.show_stock = show_stock  # False = alertas não mostram a quantidade em estoque
        self._group_photo: Optional[bytes] = None  # foto de perfil do grupo (última reserva)
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

    def send_text(self, text: str, preview: bool = False, extra: Optional[dict] = None):
        return self.call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": not preview}),
            **(extra or {}),
        })

    def send_photo(self, photo_url: str, text: str, extra: Optional[dict] = None):
        data = {"chat_id": self.chat_id, "caption": text, "parse_mode": "HTML", **(extra or {})}
        try:
            return self.call("sendPhoto", {**data, "photo": photo_url})
        except TelegramError:
            pass
        # O Telegram não conseguiu baixar a imagem: baixamos nós e enviamos o arquivo.
        content = download_photo(photo_url)
        if content is None:
            raise TelegramError("não foi possível baixar a imagem")
        return self.call("sendPhoto", data, files={"photo": ("produto.jpg", content)})

    def group_photo(self) -> Optional[bytes]:
        """Foto de perfil do grupo, baixada uma vez: última reserva para post sem foto."""
        if self._group_photo is None:
            self._group_photo = b""
            try:
                chat = self.call("getChat", {"chat_id": self.chat_id}) or {}
                file_id = (chat.get("photo") or {}).get("big_file_id")
                path = (self.call("getFile", {"file_id": file_id}) or {}).get("file_path") if file_id else None
                if path:
                    r = requests.get(f"{API}/file/bot{self.token}/{path}", timeout=30)
                    if r.ok and r.content and len(r.content) <= 10 * 1024 * 1024:
                        self._group_photo = _as_jpeg(r.content)
            except Exception as e:
                logger.warning(f"foto do grupo indisponível: {self.redact(str(e))}")
        return self._group_photo or None

    def photo_bytes(self, a: Alert) -> Optional[bytes]:
        """Foto do produto (ou .webp convertido) → logo da loja → foto do grupo."""
        for source in (a.image, a.fallback_image):
            if source:
                content = download_photo(source)
                if content:
                    return content
        return self.group_photo()

    def send_album(self, alerts: List[Alert], header: str) -> List[Alert]:
        """Álbum com as fotos enviadas como arquivo (um link .webp ou quebrado não derruba o álbum).
        Devolve os alertas que ficaram de fora por não ter nenhuma foto."""
        items, files, left_out = [], {}, []
        for a in alerts:
            content = self.photo_bytes(a)
            if content:
                files[f"p{len(items)}"] = (f"p{len(items)}.jpg", content)
                items.append(a)
            else:
                left_out.append(a)
        if len(items) < 2:  # álbum precisa de 2 fotos ou mais
            for a in items:
                self.send_alert(a)
            return left_out
        lines = [header, group_tags(items)]
        for i, a in enumerate(items, start=1):
            lines.append(compact_line(a, i, self.urgent_stock, self.show_stock))
        text = "\n".join(lines)
        while visible_len(text) > CAPTION_LIMIT and len(lines) > 3:
            lines.pop()
            text = "\n".join(lines + ["…"])
        media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(items))]
        media[0].update({"caption": text, "parse_mode": "HTML"})
        sent = self.call("sendMediaGroup", {"chat_id": self.chat_id, "media": json.dumps(media)}, files=files)
        if isinstance(sent, list):
            for a, msg in zip(items, sent):
                a.message_id = (msg or {}).get("message_id")
        return left_out

    def send_digest(self, alerts: List[Alert], silent: bool = False):
        """Resumo só em texto, dividido em mensagens dentro do limite do Telegram."""
        by_store: Dict[str, List[Alert]] = {}
        for a in alerts:
            by_store.setdefault(a.store, []).append(a)
        chunks: List[tuple] = []  # (texto, alertas daquele texto)
        text, members = "", []
        for store, items in by_store.items():
            block = [(_summary_header(store, len(items)) + "\n" + group_tags(items), None)]
            block += [(compact_line(a, urgent_stock=self.urgent_stock, show_stock=self.show_stock), a) for a in items]
            for line, a in block:
                if text and len(text) + len(line) + 1 > MESSAGE_LIMIT - 100:
                    chunks.append((text, members))
                    text, members = "", []
                text = f"{text}\n{line}" if text else line
                if a is not None:
                    members.append(a)
            text += "\n"
        if text.strip():
            chunks.append((text, members))
        extra = {"disable_notification": "true"} if silent else None
        for chunk, members in chunks:
            msg = self.send_text(chunk.strip(), extra=extra) or {}
            for a in members:
                a.message_id = msg.get("message_id")

    # --- alertas

    def _extra(self, a: Alert, with_buttons: bool = True) -> dict:
        extra = {}
        markup = buttons(a, self.urgent_stock) if with_buttons else None
        if markup:
            extra["reply_markup"] = json.dumps(markup)
        if a.kind in SILENT:
            extra["disable_notification"] = "true"
        if a.reply_to:
            extra["reply_parameters"] = json.dumps({"message_id": a.reply_to, "allow_sending_without_reply": True})
        return extra

    def _send_one(self, a: Alert, text: str, extra: dict):
        if not a.is_store_alert and a.kind != SOLD_OUT:
            # foto do produto (link direto, depois .webp convertido) → logo da loja → foto do grupo
            for source in (a.image, a.fallback_image):
                if not source:
                    continue
                try:
                    return self.send_photo(source, text, extra)
                except TelegramError as e:
                    logger.warning(f"foto não enviada ({_who(a)}): {e}")
            group = self.group_photo()
            if group:
                try:
                    return self.call("sendPhoto", {"chat_id": self.chat_id, "caption": text, "parse_mode": "HTML",
                                                   **extra}, files={"photo": ("grupo.jpg", group)})
                except TelegramError as e:
                    logger.warning(f"foto do grupo não enviada: {e}")
            logger.warning(f"sem nenhuma foto para {_who(a)}: indo só com texto")
        preview = not a.is_store_alert and a.kind != SOLD_OUT
        return self.send_text(text, preview=preview, extra=extra)

    def send_alert(self, a: Alert):
        try:
            msg = self._send_one(a, caption(a, self.urgent_stock, show_stock=self.show_stock), self._extra(a))
        except TelegramError as e:
            if "button" not in str(e).lower() and "reply_markup" not in str(e).lower():
                raise
            # botão recusado (link inválido?): manda sem botões, com o link no texto
            msg = self._send_one(a, caption(a, self.urgent_stock, with_link=True, show_stock=self.show_stock), self._extra(a, with_buttons=False))
        a.message_id = (msg or {}).get("message_id") if isinstance(msg, dict) else None
        return msg

    def send_alerts(self, alerts: List[Alert], max_individual: int = 10) -> List[Alert]:
        """Envia tudo e devolve os alertas que falharam (para tentar de novo na próxima rodada)."""
        failed: List[Alert] = []
        for a in [a for a in alerts if a.kind == RETRACT]:  # produto retirado: apaga o alerta antigo
            try:
                self.call("deleteMessage", {"chat_id": self.chat_id, "message_id": a.reply_to}, retries=1)
            except TelegramError as e:
                logger.warning(f"não consegui apagar o alerta de {_who(a)}: {e}")
        alerts = [a for a in alerts if a.kind != RETRACT]
        store_alerts = [a for a in alerts if a.is_store_alert]
        sold_out = [a for a in alerts if a.kind == SOLD_OUT]
        product_alerts = [a for a in alerts if not a.is_store_alert and a.kind != SOLD_OUT]

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
        else:
            failed += self._send_grouped(product_alerts)

        # "Esgotou": cada um responde ao seu alerta; se forem muitos, vira um resumo silencioso
        if len(sold_out) <= max_individual:
            for a in sold_out:
                try:
                    self.send_alert(a)
                except TelegramError:
                    failed.append(a)
        elif sold_out:
            try:
                self.send_digest(sold_out, silent=True)
            except TelegramError:
                failed += sold_out
        return failed

    def _send_grouped(self, product_alerts: List[Alert]) -> List[Alert]:
        """Muitos alertas: álbuns de até 10 fotos por loja, com a lista numerada na legenda."""
        by_store: Dict[str, List[Alert]] = {}
        for a in product_alerts:
            by_store.setdefault(a.store, []).append(a)
        budget = MAX_ALBUM_ITEMS
        leftovers: List[Alert] = []
        for store, items in by_store.items():
            items.sort(key=lambda a: KIND_ORDER.get(a.kind, 9))
            take = items[:budget]
            budget -= len(take)
            leftovers += items[len(take):]
            chunks = [take[i:i + ALBUM_SIZE] for i in range(0, len(take), ALBUM_SIZE)]
            for n, chunk in enumerate(chunks, start=1):
                part = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
                try:
                    if len(chunk) == 1:
                        self.send_alert(chunk[0])
                    else:
                        leftovers += self.send_album(chunk, _summary_header(store, len(items), part))
                except TelegramError:
                    leftovers += chunk
        if leftovers:
            try:
                self.send_digest(leftovers)
            except TelegramError:
                return leftovers
        return []

    def get_updates(self) -> list:
        return self.call("getUpdates", {"timeout": 0}) or []

    def _migrated_ids(self, allowed) -> set:
        """IDs novos de grupos configurados (grupos comuns, sem -100) que viraram supergrupo."""
        out = set()
        for chat_id in allowed:
            if str(chat_id).startswith("-") and not str(chat_id).startswith("-100"):
                new = _migration_target(self.token, str(chat_id))
                if new:
                    out.add(new)
        return out

    def leave_foreign_chats(self, allowed: Optional[List[str]] = None) -> List[str]:
        """Garante que o bot só funcione no grupo configurado.

        Sai de qualquer outro grupo em que alguém tenha adicionado o bot (canais só geram
        aviso no log, porque podem ser o seu próprio canal sendo montado). Travas:
        - um supergrupo criado a partir do grupo configurado (migração) é mantido;
        - se o bot não estiver no grupo configurado (ID errado?), não sai de nada.
        Devolve a lista de chats que o bot deixou.
        """
        updates = self.call("getUpdates", {
            "timeout": 0, "allowed_updates": json.dumps(["my_chat_member", "message"])}) or []
        if not updates:
            return []
        allowed = {self.chat_id, *(str(c) for c in (allowed or []))}
        for u in updates:
            msg = u.get("message") or {}
            if str(msg.get("migrate_from_chat_id")) in allowed:
                allowed.add(str(msg["chat"]["id"]))
        foreign: Dict[str, str] = {}
        for u in updates:  # em ordem: o último evento de cada chat vale
            m = u.get("my_chat_member")
            if not m or m["chat"]["type"] == "private" or str(m["chat"]["id"]) in allowed:
                continue
            if m["chat"]["type"] == "channel":
                # Pode ser o seu canal novo: não sai, só avisa (o bot não posta lá sem o chat_id).
                if m["new_chat_member"]["status"] in ("member", "administrator"):
                    logger.warning(f"o bot foi adicionado ao canal {clean_text(m['chat'].get('title'))} "
                                   f"({m['chat']['id']}); se for seu, atualize o TELEGRAM_CHAT_ID")
                continue
            cid = str(m["chat"]["id"])
            if m["new_chat_member"]["status"] in ("member", "administrator", "restricted"):
                foreign[cid] = m["chat"].get("title") or m["chat"]["type"]
            else:
                foreign.pop(cid, None)
        if foreign:
            # Um grupo configurado que virou supergrupo ganha outro ID (ex.: ao promover alguém a
            # admin). Não sai dele: só avisa para atualizar o chat_id.
            migrated = self._migrated_ids(allowed)
            for cid in [c for c in foreign if c in migrated]:
                logger.warning(f"um grupo configurado virou supergrupo (novo ID {cid}): atualize o chat_id nos Secrets")
                foreign.pop(cid)
        left = []
        if foreign:
            me = self.call("getMe")
            status = (self.call("getChatMember", {"chat_id": self.chat_id, "user_id": me["id"]}) or {}).get("status")
            if status not in ("member", "administrator", "creator", "restricted"):
                logger.warning("o bot não está no grupo configurado; por segurança não vou sair de outros chats")
            else:
                for cid, title in foreign.items():
                    try:
                        self.call("leaveChat", {"chat_id": cid})
                        left.append(f"{title} ({cid})")
                    except TelegramError as e:
                        logger.warning(f"não consegui sair de {title}: {e}")
        # confirma as atualizações lidas, para não reprocessá-las
        self.call("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})
        return left


WEBP_ALT_RE = re.compile(r"(-\d+-\d+)\.jpe?g(\?.*)?$", re.I)


def download_photo(url: str) -> Optional[bytes]:
    """Baixa a foto em JPEG. Fotos novas da Nuvemshop só existem em .webp (o .jpg dá 403):
    nesse caso baixa a versão .webp e converte, porque o Telegram não aceita .webp como foto."""
    candidates = [url]
    alt = WEBP_ALT_RE.sub(r"\1.webp\2", url)
    if alt != url:
        candidates.append(alt)
    for candidate in candidates:
        content = _download(candidate)
        if content:
            return _as_jpeg(content)
    return None


def _as_jpeg(content: bytes) -> bytes:
    if content[:3] == b"\xff\xd8\xff" or content[:8] == b"\x89PNG\r\n\x1a\n":
        return content  # JPEG ou PNG: o Telegram aceita como está
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000  # recusa imagens absurdamente grandes
        with Image.open(io.BytesIO(content)) as img:
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=90)
            return out.getvalue()
    except Exception:
        return content


def _migration_target(token: str, chat_id: str) -> Optional[str]:
    """Novo ID de um grupo comum que virou supergrupo (ou None). Não segue a migração."""
    try:
        r = requests.post(f"{API}/bot{token}/sendChatAction", data={"chat_id": chat_id, "action": "typing"},
                          timeout=20).json()
    except Exception:
        return None
    new = (r.get("parameters") or {}).get("migrate_to_chat_id")
    return str(new) if new else None


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
