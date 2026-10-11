"""Compara a varredura atual com o estado salvo e gera os alertas (funções puras)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .config import StoreConfig
from .models import (LOW_STOCK, NEW, PRICE, RESTOCK, VARIANT_RESTOCK, Alert, Product,
                     ScanResult, Variant)

# Produto some da listagem em N varreduras completas seguidas → tratado como indisponível
# (lojas que escondem esgotados). Assim, quando ele reaparecer, vira alerta de restock.
MISSING_TO_UNAVAILABLE = 2
FORGET_AFTER_DAYS = 120
VARIANT_REFRESH_HOURS = 6
MAX_STALE_REFRESH = 5


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
                alerts.append(_alert(NEW, store, p, variants=_available_variants(p)))
            continue

        was_available = bool(prev.get("available"))
        old_stock = prev.get("stock")

        if not was_available and p.available:
            if cfg.restock:
                note = "Voltou ao catálogo da loja" if prev.get("missing") else ""
                alerts.append(_alert(RESTOCK, store, p, variants=_available_variants(p), note=note))
        elif was_available and p.available and cfg.restock and p.variants and prev.get("variants"):
            old_vars = prev["variants"]
            back = [v for vid, v in p.variants.items()
                    if v.available and (vid not in old_vars or not old_vars[vid].get("available"))]
            if back:
                alerts.append(_alert(VARIANT_RESTOCK, store, p, variants=[_variant_label(v) for v in back]))

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
            if rec["missing"] >= MISSING_TO_UNAVAILABLE:
                rec["available"] = False
            if _days_since(rec["missing_since"], now.date()) > FORGET_AFTER_DAYS:
                continue  # removido há muito tempo: esquece
        products[pid] = rec

    return alerts, products


def _alert(kind: str, store: StoreConfig, p: Product, **extra) -> Alert:
    return Alert(kind=kind, store=store.name, store_url=store.url, name=p.name, url=p.url,
                 image=p.image, price=p.price, stock=p.stock, available=p.available, **extra)


def _variant_label(v: Variant) -> str:
    label = v.name or "única"
    return f"{label} ({v.stock})" if isinstance(v.stock, int) else label


def _available_variants(p: Product) -> List[str]:
    if not p.variants or len(p.variants) < 2:
        return []
    return [_variant_label(v) for v in p.variants.values() if v.available]


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
