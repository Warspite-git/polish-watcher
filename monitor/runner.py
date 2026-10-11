"""Orquestra uma rodada: varre as lojas em paralelo, compara, salva o estado e envia os alertas."""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from . import state as st
from .adapters import ADAPTERS, detect_platform
from .config import Settings, StoreConfig
from .diff import baseline, diff_products, select_variant_fetches
from .fetcher import Fetcher
from .models import (LOW_STOCK, NEW, RESTOCK, STORE_CLOSED, STORE_ERROR, STORE_OPEN, STORE_RECOVERED,
                     VARIANT_RESTOCK, Alert, clean_text, describe_counts)
from .telegram import Telegram, caption

logger = logging.getLogger("monitor")

OUTBOX_MAX_AGE = timedelta(hours=24)
OUTBOX_MAX_SIZE = 50


class StoreLog(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['store']}] {clean_text(msg)}", kwargs


@dataclass
class Outcome:
    store: StoreConfig
    alerts: List[Alert] = field(default_factory=list)
    status: str = "?"
    products: int = 0
    available: int = 0
    requests: int = 0
    seconds: float = 0.0
    note: str = ""
    error: Optional[str] = None


def process_store(store: StoreConfig, settings: Settings, now: datetime, state_dir: Path, save: bool) -> Outcome:
    log = StoreLog(logger, {"store": store.name})
    out = Outcome(store)
    t0 = time.monotonic()
    state = st.load(store.key, state_dir)
    fetcher = Fetcher(delay=store.delay, allowed_host=store.key)
    now_iso = now.isoformat(timespec="seconds")

    def finish() -> Outcome:
        out.requests = fetcher.requests
        out.seconds = time.monotonic() - t0
        if save:
            st.save(store.key, state, state_dir)
        return out

    try:
        platform = store.platform
        if platform == "auto":
            platform = state.get("platform") or detect_platform(fetcher, store.url)
        adapter = ADAPTERS[platform](store, fetcher, log)
        log.info(f"lendo catálogo ({platform})…")
        scan = adapter.scan()
    except Exception as e:  # uma loja com problema não pode derrubar as outras
        state["errors"] = int(state.get("errors") or 0) + 1
        out.status, out.error = "erro", str(e)
        log.error(f"falhou ({state['errors']}ª vez seguida): {e}")
        if state["errors"] == settings.error_alert_after:
            out.alerts.append(Alert(kind=STORE_ERROR, store=store.name, store_url=store.url, store_key=store.key, store_tag=store.hashtag,
                                    note=f"{state['errors']} tentativas seguidas falharam. Último erro: {str(e)[:300]}"))
        if store.platform == "auto" and state["errors"] >= 3:
            state["platform"] = None  # tenta detectar a plataforma de novo
        return finish()

    state["platform"] = platform
    if int(state.get("errors") or 0) >= settings.error_alert_after:
        out.alerts.append(Alert(kind=STORE_RECOVERED, store=store.name, store_url=store.url, store_key=store.key,
                                store_tag=store.hashtag))
    state["errors"] = 0
    prev_status = state.get("status")
    state["status"] = out.status = scan.status

    if scan.status == "password":
        out.note = "fechada com senha"
        log.info("loja fechada com senha (/password)")
        if prev_status == "open" and store.alerts.store_status:
            out.alerts.append(Alert(kind=STORE_CLOSED, store=store.name, store_url=store.url, store_key=store.key, store_tag=store.hashtag,
                                    note="A loja foi fechada com senha. Pode ser lançamento chegando! 👀"))
        return finish()

    out.products = len(scan.products)
    out.available = sum(1 for p in scan.products.values() if p.available)
    log.info(f"{out.products} produtos lidos ({out.available} disponíveis)"
             + ("" if scan.complete else " — varredura INCOMPLETA"))

    if prev_status == "password" and store.alerts.store_status:
        out.alerts.append(Alert(kind=STORE_OPEN, store=store.name, store_url=store.url, store_key=store.key, store_tag=store.hashtag,
                                note=f"A loja reabriu com {out.available} produtos disponíveis."))

    if not state.get("baseline"):
        if not scan.complete:
            out.note = "linha de base adiada (varredura incompleta)"
            log.warning(out.note)
            return finish()
        state["products"] = baseline(scan, now)
        state["baseline"] = now_iso
        out.note = "linha de base registrada (sem alertas)"
        log.info(f"primeira execução: {out.note}")
        return finish()

    fetched = set()
    if not adapter.variants_in_listing:
        ids = select_variant_fetches(state["products"], scan, settings.max_product_pages, now, store.wants)
        for pid in ids:
            p = scan.products[pid]
            try:
                variants = adapter.fetch_variants(pid, p.url)
            except Exception as e:
                log.warning(f"não consegui ler variantes de '{p.name}': {e}")
                continue
            if variants:
                p.variants = variants
                fetched.add(pid)
        if ids:
            log.info(f"{len(fetched)}/{len(ids)} páginas de produto abertas para conferir variantes")

    alerts, products = diff_products(store, state["products"], scan, now, fetched)
    state["products"] = products
    out.alerts.extend(alerts)
    out.note = describe_counts(a.kind for a in alerts) or "sem novidades"
    log.info(out.note)
    return finish()


def run(settings: Settings, only: Optional[str] = None, dry_run: bool = False,
        state_dir: Path = st.STATE_DIR, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    stores = [s for s in settings.stores if s.enabled]
    if only:
        q = only.lower()
        stores = [s for s in stores if q in s.key or q in s.name.lower()]
        if not stores:
            logger.error(f"Nenhuma loja corresponde a '{only}'")
            return 2
    save = not dry_run

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(stores)))) as pool:
        outcomes = list(pool.map(lambda s: process_store(s, settings, now, state_dir, save), stores))

    alerts = [a for o in outcomes for a in o.alerts]
    for a in alerts:
        a.created = now.isoformat(timespec="seconds")
    _print_summary(outcomes, len(alerts))

    if dry_run:
        _print_alerts(alerts, settings)
    else:
        _deliver(alerts, settings, state_dir, now)

    _github_output(outcomes, alerts)
    return 1 if outcomes and all(o.error for o in outcomes) else 0


def _deliver(alerts: List[Alert], settings: Settings, state_dir: Path, now: datetime):
    pending = [a for a in st.load_outbox(state_dir) if _recent(a, now)]
    if pending:
        logger.info(f"reenviando {len(pending)} alerta(s) que falharam na rodada anterior")
    queue = pending + alerts
    token, chat_id = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        if queue:
            logger.warning(f"TELEGRAM_TOKEN/TELEGRAM_CHAT_ID não configurados: {len(queue)} alerta(s) guardado(s) para depois")
        st.save_outbox(queue[-OUTBOX_MAX_SIZE:], state_dir)
        return
    tg = Telegram(token, chat_id, urgent_stock=settings.urgent_stock)
    try:  # o bot só funciona no grupo configurado: sai de qualquer outro
        for chat in tg.leave_foreign_chats():
            logger.warning(f"o bot foi adicionado a outro chat e saiu: {clean_text(chat)}")
    except Exception as e:
        logger.warning(f"verificação de chats do Telegram falhou: {tg.redact(str(e))}")
    if not queue:
        st.save_outbox([], state_dir)
        return
    try:
        failed = tg.send_alerts(queue, settings.max_individual_alerts)
    except Exception as e:  # nunca perder os alertas por um erro inesperado
        logger.error(f"Telegram: {tg.redact(str(e))}")
        failed = queue
    if failed:
        logger.warning(f"{len(failed)} alerta(s) não enviado(s); nova tentativa na próxima rodada")
    else:
        logger.info(f"{len(queue)} alerta(s) enviado(s) ao Telegram")
    st.save_outbox(failed[-OUTBOX_MAX_SIZE:], state_dir)
    wants_sold_out = {s.key for s in settings.stores if s.alerts.sold_out}
    _remember_messages([a for a in queue if a not in failed and a.store_key in wants_sold_out], state_dir, now)


ANNOUNCE_KINDS = {NEW, RESTOCK, VARIANT_RESTOCK, LOW_STOCK}


def _remember_messages(sent: List[Alert], state_dir: Path, now: datetime):
    """Guarda no estado qual mensagem anunciou cada produto, para responder \"⚡ esgotou\" depois."""
    by_store: dict = {}
    for a in sent:
        if a.kind in ANNOUNCE_KINDS and a.available and a.message_id and a.store_key and a.product_id:
            by_store.setdefault(a.store_key, []).append(a)
    for key, items in by_store.items():
        state = st.load(key, state_dir)
        for a in items:
            rec = (state.get("products") or {}).get(a.product_id)
            if rec is None or not rec.get("available"):
                continue
            if a.kind == LOW_STOCK and rec.get("alert"):
                continue  # "esgotou" responde ao anúncio original (novidade/restock), não ao "últimas unidades"
            rec["alert"] = {"msg": a.message_id, "at": now.isoformat(timespec="seconds")}
        st.save(key, state, state_dir)


def _recent(a: Alert, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(a.created) <= OUTBOX_MAX_AGE
    except (TypeError, ValueError):
        return False


def _print_summary(outcomes: List[Outcome], n_alerts: int):
    logger.info("=" * 78)
    logger.info(f"{'Loja':<22}{'Status':<9}{'Produtos':>9}{'Disp.':>7}{'Req.':>6}{'Tempo':>7}  Resultado")
    for o in outcomes:
        result = o.error[:60] if o.error else o.note
        logger.info(f"{o.store.name[:21]:<22}{o.status:<9}{o.products:>9}{o.available:>7}{o.requests:>6}"
                    f"{o.seconds:>6.0f}s  {result}")
    logger.info(f"Total de alertas nesta rodada: {n_alerts}")
    logger.info("=" * 78)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        rows = ["| Loja | Status | Produtos | Disponíveis | Requisições | Tempo | Resultado |",
                "|---|---|---:|---:|---:|---:|---|"]
        for o in outcomes:
            result = f"❌ {o.error[:120]}" if o.error else o.note
            rows.append(f"| {o.store.name} | {o.status} | {o.products} | {o.available} | {o.requests} "
                        f"| {o.seconds:.0f}s | {result} |")
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"### Polish Watcher — {n_alerts} alerta(s)\n\n" + "\n".join(rows) + "\n")


def _print_alerts(alerts: List[Alert], settings: Settings):
    if not alerts:
        logger.info("[dry-run] nenhum alerta")
        return
    logger.info(f"[dry-run] {len(alerts)} alerta(s) que seriam enviados:")
    for a in alerts:
        print("-" * 60)
        print(caption(a, settings.urgent_stock))
        if a.image:
            print(f"(foto: {a.image})")
    print("-" * 60)


def _github_output(outcomes: List[Outcome], alerts: List[Alert]):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    summary = describe_counts(a.kind for a in alerts) or "sem alertas"
    errors = sum(1 for o in outcomes if o.error)
    if errors:
        summary += f", {errors} loja(s) com erro"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"summary={summary}\n")
