"""Novidades recentes: produtos que entraram nas lojas nos últimos N dias.

Útil para mandar um resumo do que foi lançado antes de o monitor existir, ou para
quem entrou no grupo agora. Só dá para recuperar PRODUTOS NOVOS: as lojas não
guardam histórico de estoque, então restocks passados não podem ser reconstruídos.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from . import state as st
from .adapters import ADAPTERS, detect_platform
from .config import Settings, StoreConfig
from .fetcher import Fetcher
from .models import NEW, Alert, Product

BR = ZoneInfo("America/Sao_Paulo")
logger = logging.getLogger("monitor")


def _store_products(store: StoreConfig) -> List[Product]:
    fetcher = Fetcher(delay=store.delay, allowed_host=store.key)
    platform = store.platform
    if platform == "auto":
        platform = st.load(store.key).get("platform") or detect_platform(fetcher, store.url)
    scan = ADAPTERS[platform](store, fetcher).scan()
    if scan.status != "open":
        logger.info(f"[{store.name}] loja fechada com senha; pulando")
        return []
    return list(scan.products.values())


def collect_recent(settings: Settings, days: float, now: Optional[datetime] = None, only: Optional[str] = None,
                   available_only: bool = False) -> List[Alert]:
    """Alertas de "produto novo" para o que entrou nos últimos `days` dias, do mais antigo ao mais novo."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    stores = [s for s in settings.stores if s.enabled]
    if only:
        q = only.lower()
        stores = [s for s in stores if q in s.key or q in s.name.lower()]

    def work(store):
        try:
            return store, _store_products(store)
        except Exception as e:  # uma loja com problema não impede as outras
            logger.error(f"[{store.name}] falhou: {e}")
            return store, []

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(stores)))) as pool:
        results = list(pool.map(work, stores))

    found = []
    for store, products in results:
        for p in products:
            if not p.added_at or not store.wants(p.name) or (available_only and not p.available):
                continue
            added = datetime.fromisoformat(p.added_at)
            if cutoff <= added <= now + timedelta(days=1):
                found.append((added, store, p))
    found.sort(key=lambda item: item[0])

    alerts = []
    for added, store, p in found:
        local = added.astimezone(BR)
        when = f"{local:%d/%m às %H:%M}" if p.added_at_exact else f"{local:%d/%m} (aprox.)"
        alerts.append(Alert(kind=NEW, store=store.name, store_url=store.url, name=p.name, url=p.url,
                            image=p.image, price=p.price, stock=p.stock, available=p.available,
                            note=f"🗓️ Adicionado em {when}", created=now.isoformat(timespec="seconds")))
    return alerts
