"""Novidades recentes: produtos que entraram nas lojas nos últimos N dias.

Útil para mandar um resumo do que foi lançado antes de o monitor existir, ou para
quem entrou no grupo agora. Só dá para recuperar PRODUTOS NOVOS: as lojas não
guardam histórico de estoque, então restocks passados não podem ser reconstruídos.

A data vem do nome da foto do produto. Produto sem foto (ou com foto reaproveitada de
antes) usa o dia em que o monitor o viu pela primeira vez, se foi depois da linha de base.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from . import state as st
from .adapters import ADAPTERS, detect_platform
from .config import Settings, StoreConfig
from .fetcher import Fetcher
from .models import NEW, Alert, Product
from .runner import _fill_missing_photos

BR = ZoneInfo("America/Sao_Paulo")
logger = logging.getLogger("monitor")


def _saved_state(store: StoreConfig, state_dir: Path) -> dict:
    try:
        return st.load(store.key, state_dir, private=store.private)
    except Exception:  # loja privada sem STATE_KEY, arquivo corrompido...
        return st.new_state()


def _seen_at(rec: dict, baseline: Optional[str]) -> Optional[str]:
    """Quando o monitor viu o produto pela primeira vez, se foi depois da linha de base."""
    first = rec.get("first_seen")
    if not first or not baseline or first <= baseline[:10]:
        return None  # já existia quando o monitor começou: data desconhecida
    at = (rec.get("alert") or {}).get("at") or ""
    return at if at[:10] == first else f"{first}T12:00:00+00:00"


def _date_from_history(products: List[Product], saved: dict):
    records = saved.get("products") or {}
    for p in products:
        seen = _seen_at(records.get(p.id) or {}, saved.get("baseline"))
        if seen and (not p.added_at or p.added_at < seen[:10]):  # sem foto, ou foto de antes
            p.added_at, p.added_at_exact = seen, False


def _store_products(store: StoreConfig, problems: Optional[list] = None,
                    state_dir: Path = st.STATE_DIR) -> List[Product]:
    fetcher = Fetcher(delay=store.delay, allowed_host=store.key)
    saved = _saved_state(store, state_dir)
    platform = store.platform
    if platform == "auto":
        platform = saved.get("platform") or detect_platform(fetcher, store.url)
    scan = ADAPTERS[platform](store, fetcher).scan()
    if not scan.complete and problems is not None:
        problems.append(f"{store.name}: leitura incompleta")
    if scan.status != "open":
        logger.info(f"[{store.name}] loja fechada com senha; pulando")
        return []
    products = list(scan.products.values())
    _date_from_history(products, saved)
    return products


def collect_recent(settings: Settings, days: float, now: Optional[datetime] = None, only: Optional[str] = None,
                   available_only: bool = False, problems: Optional[list] = None,
                   state_dir: Path = st.STATE_DIR) -> List[Alert]:
    """Alertas de "produto novo" para o que entrou nos últimos `days` dias, do mais antigo ao mais novo.

    Lojas com leitura incompleta ou com erro são anotadas em `problems` (se informado).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    stores = [s for s in settings.stores if s.enabled]
    if only:
        q = only.lower()
        stores = [s for s in stores if q in s.key or q in s.name.lower()]

    def work(store):
        try:
            return store, _store_products(store, problems, state_dir)
        except Exception as e:  # uma loja com problema não impede as outras
            logger.error(f"[{store.name}] falhou: {e}")
            if problems is not None:
                problems.append(f"{store.name}: erro ({e})")
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
    for store in {s.key: s for _, s, _ in found}.values():  # logo da loja para quem não tem foto
        _fill_missing_photos([a for a in alerts if a.store == store.name], store,
                             Fetcher(delay=store.delay, allowed_host=store.key), logger)
    return alerts
