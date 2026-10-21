"""Compara a varredura atual com o estado salvo e gera os alertas (funções puras)."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .config import StoreConfig
from .models import (LOW_STOCK, NEW, PRICE, REPLENISH, RESTOCK, RETRACT, SOLD_OUT, VARIANT_RESTOCK, Alert, Product,
                     ScanResult, Variant)

# Produto some da listagem em N varreduras completas seguidas → tratado como indisponível
# (lojas que escondem esgotados). Assim, quando ele reaparecer, vira alerta de restock.
MISSING_TO_UNAVAILABLE = 2
FORGET_AFTER_DAYS = 120
VARIANT_REFRESH_HOURS = 6
MAX_STALE_REFRESH = 5
SOLD_OUT_WINDOW_HOURS = 48
# Produto novo sem foto (loja publicou antes de subir a imagem): espera até N verificações
# pela foto antes de anunciar. Se o produto sumir nesse meio-tempo, nada é postado.
PHOTO_WAIT_ROUNDS = 2
# O Telegram só deixa o bot apagar mensagens com menos de 48h
RETRACT_WINDOW_HOURS = 47  # só responde "esgotou" se o alerta original for recente


def record_from(p: Product, prev: Optional[dict], today: str, now_iso: str, variants_from_page: bool) -> dict:
    rec = {
        "name": p.name,
        "price": p.price,
        "available": p.available,
        "stock": p.stock,
        "url": p.url,
        "first_seen": (prev or {}).get("first_seen") or today,
    }
    if p.variants is not None:
        rec["nv"] = len(p.variants)
        if len(p.variants) > 1:
            rec["variants"] = {
                vid: {"name": v.name, "available": v.available, "stock": v.stock}
                for vid, v in p.variants.items()
            }
            if variants_from_page:
                rec["variants_checked"] = now_iso
    elif prev:
        for k in ("nv", "variants", "variants_checked"):
            if k in prev:
                rec[k] = prev[k]
    if prev and prev.get("alert") and p.available:
        rec["alert"] = prev["alert"]  # mensagem que anunciou o produto (para responder "esgotou")
    posts = recent_posts(prev or {}, datetime.fromisoformat(now_iso))
    if posts:
        rec["posts"] = posts  # todos os anúncios recentes, mesmo esgotado (para apagar se for retirado)
    return rec


def baseline(scan: ScanResult, now: datetime) -> Dict[str, dict]:
    today, now_iso = now.date().isoformat(), now.isoformat(timespec="seconds")
    return {pid: record_from(p, None, today, now_iso, False) for pid, p in scan.products.items()}


def select_variant_fetches(prev_products: Dict[str, dict], scan: ScanResult, cap: int, now: datetime,
                           wants: Callable[[str], bool] = lambda _: True) -> List[str]:
    """Escolhe quais páginas de produto abrir para ler o estoque por variante.

    - restock e aumento de estoque: pode ser uma variante que voltou;
    - produto novo: para listar as variantes no alerta;
    - produtos com várias variantes não conferidos há algumas horas.
    Produtos que já sabemos ter uma variante só nunca são reabertos.
    """
    restock, stock_up, new, stale = [], [], [], []
    for pid, p in scan.products.items():
        if not wants(p.name):
            continue
        prev = prev_products.get(pid)
        if prev is None:
            new.append(pid)
            continue
        nv = prev.get("nv")
        if nv == 1:
            continue
        was_available = bool(prev.get("available"))
        old_stock = prev.get("stock")
        if not was_available and p.available:
            restock.append(pid)
        elif (was_available and p.available and isinstance(p.stock, int)
              and isinstance(old_stock, int) and p.stock > old_stock):
            stock_up.append(pid)
        elif nv and nv > 1 and p.available and _older_than(prev.get("variants_checked"), now, VARIANT_REFRESH_HOURS):
            stale.append(pid)
    return (restock + stock_up + new + stale[:MAX_STALE_REFRESH])[:cap]


def diff_products(store: StoreConfig, prev_products: Dict[str, dict], scan: ScanResult, now: datetime,
                  variants_from_page: Iterable[str] = ()) -> Tuple[List[Alert], Dict[str, dict]]:
    today, now_iso = now.date().isoformat(), now.isoformat(timespec="seconds")
    from_page = set(variants_from_page)
    cfg = store.alerts
    alerts: List[Alert] = []
    products: Dict[str, dict] = {}

    for pid, p in scan.products.items():
        prev = prev_products.get(pid)
        products[pid] = record_from(p, prev, today, now_iso, pid in from_page)
        if not store.wants(p.name):
            continue

        if prev is None:
            if cfg.new:
                if p.image is None and p.available and PHOTO_WAIT_ROUNDS:
                    products[pid]["photo_wait"] = 1
                else:
                    alerts.append(_alert(NEW, store, p, **_variant_fields(_available_variants(p))))
            continue

        if prev.get("photo_wait"):  # produto novo que estava esperando a foto
            if p.available and (p.image or prev["photo_wait"] >= PHOTO_WAIT_ROUNDS):
                alerts.append(_alert(NEW, store, p, **_variant_fields(_available_variants(p))))
            elif p.available:
                products[pid]["photo_wait"] = prev["photo_wait"] + 1
            continue

        was_available = bool(prev.get("available"))
        old_stock = prev.get("stock")

        variant_back = False
        if not was_available and p.available:
            if cfg.restock:
                note = "Voltou ao catálogo da loja" if prev.get("missing") else ""
                alerts.append(_alert(RESTOCK, store, p, **_variant_fields(_available_variants(p)), note=note))
        elif was_available and p.available and cfg.restock and p.variants and prev.get("variants"):
            old_vars = prev["variants"]
            back = [v for vid, v in p.variants.items()
                    if v.available and (vid not in old_vars or not old_vars[vid].get("available"))]
            if back:
                alerts.append(_alert(VARIANT_RESTOCK, store, p, **_variant_fields(back)))
                variant_back = True

        # Reposição: chegaram mais unidades num produto que não chegou a esgotar (ex.: 3 → 30)
        if (cfg.replenish and was_available and p.available and not variant_back
                and isinstance(p.stock, int) and isinstance(old_stock, int)
                and p.stock - old_stock >= cfg.replenish):
            alerts.append(_alert(REPLENISH, store, p, old_stock=old_stock))

        if was_available and not p.available and cfg.sold_out:
            note = sold_out_note(prev.get("alert"), now)
            if note:
                alerts.append(_alert(SOLD_OUT, store, p, reply_targets=alert_msgs(prev["alert"]), note=note))

        threshold = cfg.low_stock
        if (threshold and was_available and p.available and isinstance(p.stock, int)
                and isinstance(old_stock, int) and old_stock > threshold >= p.stock > 0):
            alerts.append(_alert(LOW_STOCK, store, p))

        old_price = prev.get("price")
        if (cfg.price_change and p.price is not None and old_price is not None
                and abs(p.price - old_price) >= 0.01):
            alerts.append(_alert(PRICE, store, p, old_price=old_price))

    # Produtos que não apareceram nesta varredura
    for pid, prev in prev_products.items():
        if pid in scan.products:
            continue
        rec = dict(prev)
        if scan.complete:
            rec["missing"] = min(rec.get("missing", 0) + 1, MISSING_TO_UNAVAILABLE)
            rec.setdefault("missing_since", today)
            posts = recent_posts(rec, now)
            rec.pop("posts", None)
            if posts:
                rec["posts"] = posts
            if rec["missing"] >= MISSING_TO_UNAVAILABLE:
                if rec.get("available") and cfg.sold_out and store.wants(rec.get("name", "")):
                    note = sold_out_note(rec.get("alert"), now)
                    if note:  # a loja escondeu o produto: provavelmente esgotou
                        alerts.append(Alert(kind=SOLD_OUT, store=store.name, store_url=store.url,
                                            name=rec.get("name", ""), url=rec.get("url", ""), available=False,
                                            reply_targets=alert_msgs(rec["alert"]), note=note, store_key=store.key,
                                            store_tag=store.hashtag, product_id=pid, private=store.private))
                # produto anunciado sumiu: o runner confere se a página dá 404 antes de apagar os alertas.
                # Enquanto a página estiver no ar (só escondido), confere de novo a cada rodada.
                for post in posts:
                    alerts.append(Alert(kind=RETRACT, store=store.name, store_url=store.url, name=rec.get("name", ""),
                                        url=rec.get("url", ""), reply_targets=alert_msgs(post), store_key=store.key,
                                        product_id=pid, private=store.private))
                rec["available"] = False
                rec.pop("alert", None)
            if _days_since(rec["missing_since"], now.date()) > FORGET_AFTER_DAYS:
                continue  # removido há muito tempo: esquece
        products[pid] = rec

    return alerts, products


def _alert(kind: str, store: StoreConfig, p: Product, **extra) -> Alert:
    return Alert(kind=kind, store=store.name, store_url=store.url, name=p.name, url=p.url,
                 image=p.image, price=p.price, stock=p.stock, available=p.available,
                 store_key=store.key, store_tag=store.hashtag, product_id=p.id, private=store.private, **extra)


def alert_msgs(sent: Optional[dict]) -> Dict[str, int]:
    """Mensagens que anunciaram o produto, por grupo ({"principal": 123, "privado": 45})."""
    if not sent:
        return {}
    if sent.get("msgs"):
        return {k: v for k, v in sent["msgs"].items() if v}
    return {"principal": sent["msg"]} if sent.get("msg") else {}  # formato antigo: só o grupo principal


def recent_posts(rec: dict, now: datetime) -> List[dict]:
    """Mensagens que anunciaram o produto nas últimas RETRACT_WINDOW_HOURS (o bot ainda pode apagá-las)."""
    posts = rec.get("posts")
    if posts is None:  # estado antigo: só guardava o último anúncio de produto disponível
        posts = [rec["alert"]] if rec.get("alert") else []
    return [p for p in posts if alert_msgs(p) and not _older_than(p.get("at"), now, RETRACT_WINDOW_HOURS)]


def sold_out_note(sent: Optional[dict], now: datetime) -> Optional[str]:
    """Texto "Esgotou em menos de X" a partir do alerta original; None se não houver ou for antigo.

    O monitor só percebe a mudança na rodada seguinte, então o tempo é um limite superior.
    """
    if not alert_msgs(sent):
        return None
    try:
        elapsed = now - datetime.fromisoformat(sent["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if elapsed < timedelta(0) or elapsed > timedelta(hours=SOLD_OUT_WINDOW_HOURS):
        return None
    minutes = elapsed.total_seconds() / 60
    if minutes < 55:
        return f"Esgotou em menos de {max(5, math.ceil(minutes / 5) * 5)} minutos"
    hours = max(1, math.ceil(minutes / 60))
    return f"Esgotou em menos de {hours} hora{'s' if hours > 1 else ''}"


def _variant_fields(variants: List[Variant]) -> dict:
    """Nomes das variantes e, à parte, o estoque de cada uma: o grupo público não mostra estoque."""
    return {"variants": [v.name or "única" for v in variants],
            "variant_stock": [v.stock if isinstance(v.stock, int) else None for v in variants]}


def _available_variants(p: Product) -> List[Variant]:
    if not p.variants or len(p.variants) < 2:
        return []
    return [v for v in p.variants.values() if v.available]


def _older_than(iso: Optional[str], now: datetime, hours: float) -> bool:
    if not iso:
        return True
    try:
        return now - datetime.fromisoformat(iso) > timedelta(hours=hours)
    except (TypeError, ValueError):
        return True


def _days_since(iso_date: str, today: date) -> int:
    try:
        return (today - date.fromisoformat(iso_date)).days
    except (TypeError, ValueError):
        return 0
