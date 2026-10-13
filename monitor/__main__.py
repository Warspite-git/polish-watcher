"""Uso:
    python -m monitor                      roda todas as lojas
    python -m monitor run --store whatcha  roda só uma loja
    python -m monitor run --dry-run        mostra os alertas sem enviar e sem salvar o estado
    python -m monitor chat-id              lista os chats que falaram com o bot
    python -m monitor test-telegram        envia uma mensagem de teste
    python -m monitor detect <url>         detecta a plataforma de uma loja
    python -m monitor recentes --dias 3    lista o que entrou nas lojas nos últimos dias
    python -m monitor recentes --dias 3 --enviar   ...e envia um alerta por produto
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_settings, normalize_url
from .fetcher import Fetcher
from .state import STATE_DIR


def load_dotenv(path: str = ".env"):
    """Carrega o .env local (sem sobrescrever variáveis já definidas)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def _telegram():
    from .telegram import Telegram
    token, chat_id = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        sys.exit("TELEGRAM_TOKEN não definido (coloque no .env ou nos Secrets do GitHub)")
    return Telegram(token, chat_id or "")


def cmd_run(args) -> int:
    from .runner import run
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        logging.error(f"sites.yaml: {e}")
        return 2
    return run(settings, only=args.store, dry_run=args.dry_run, state_dir=Path(args.state_dir))


def _chats_from_updates(updates) -> dict:
    chats = {}
    for u in updates:
        for key in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    return chats


def _save_env(key: str, value: str, path: str = ".env"):
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_chat_id(args) -> int:
    import time
    tg = _telegram()
    me = tg.call("getMe")
    print(f"Bot: @{me['username']}  →  https://t.me/{me['username']}")
    deadline = time.monotonic() + args.wait
    chats = _chats_from_updates(tg.get_updates())
    if not chats and args.wait:
        print(f"Esperando até {args.wait}s: abra o link acima e toque em INICIAR (ou mande /start)…", flush=True)
    while not chats and time.monotonic() < deadline:
        chats = _chats_from_updates(tg.call("getUpdates", {"timeout": 20}) or [])
    if not chats:
        print("Nenhuma mensagem encontrada. Mande /start para o bot (link acima) e rode de novo.")
        return 1
    print("Chats que falaram com o bot:")
    for cid, c in chats.items():
        label = c.get("title") or " ".join(filter(None, [c.get("first_name"), c.get("last_name")]))
        user = f" @{c['username']}" if c.get("username") else ""
        print(f"  chat_id = {cid}   ({c.get('type')}: {label}{user})")
    if args.save:
        if len(chats) == 1:
            _save_env("TELEGRAM_CHAT_ID", str(next(iter(chats))))
            print("TELEGRAM_CHAT_ID gravado no .env")
        else:
            print("Mais de um chat: copie o chat_id desejado para o .env manualmente.")
    return 0


def cmd_test_telegram(args) -> int:
    from .telegram import TelegramError
    tg = _telegram()
    try:
        settings = load_settings(args.config)
        tg.urgent_stock, tg.show_stock = settings.urgent_stock, settings.show_stock
    except ConfigError:
        pass
    if not tg.chat_id:
        sys.exit("TELEGRAM_CHAT_ID não definido (rode 'python -m monitor chat-id' para descobrir)")
    try:
        tg.send_text("✅ <b>Polish Watcher conectado!</b>\nOs alertas de restock e produtos novos chegarão aqui.")
        sample = _sample_alert(args.config)
        if sample:
            sample.note = "🧪 TESTE: este alerta é só um exemplo de formato. O produto existe na loja, mas NÃO voltou ao estoque agora."
            tg.send_alert(sample)
    except TelegramError as e:
        print(f"Falhou: {e}")
        return 1
    print("Mensagem de teste enviada.")
    return 0


def _sample_alert(config: str):
    """Monta um alerta de exemplo a partir de um produto real do estado salvo."""
    from .models import RESTOCK, Alert
    try:
        stores = load_settings(config).stores
    except ConfigError:
        return None
    for store in stores:
        path = STATE_DIR / f"{store.key}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for rec in (data.get("products") or {}).values():
            if rec.get("available") and rec.get("stock"):
                return Alert(kind=RESTOCK, store=store.name, store_url=store.url, name=rec["name"],
                             url=rec["url"], price=rec.get("price"), stock=rec.get("stock"))
    return None


def cmd_recentes(args) -> int:
    import time
    from .recent import collect_recent
    from .telegram import TelegramError, stock_text
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        logging.error(f"sites.yaml: {e}")
        return 2
    problems: list = []
    alerts = collect_recent(settings, args.dias, only=args.store, available_only=args.disponiveis, problems=problems)
    dias = int(args.dias) if float(args.dias).is_integer() else args.dias
    print(f"\n{len(alerts)} produto(s) adicionados nos últimos {dias} dia(s), do mais antigo ao mais novo:")
    for a in alerts:
        print(f"  {a.note.replace('🗓️ Adicionado em ', ''):18} {a.store:20} {a.name[:45]:45} {stock_text(a, settings.show_stock)}")
    if problems:
        print("\n⚠️  Lista possivelmente incompleta:\n  " + "\n  ".join(problems))
        if args.enviar and not args.forcar:
            print("Nada foi enviado. Rode de novo (a instabilidade costuma passar) ou use --forcar.")
            return 1
    if not args.enviar or not alerts:
        if alerts:
            print("\n(nada enviado; use --enviar para mandar ao Telegram)")
        return 0
    tg = _telegram()
    tg.urgent_stock, tg.show_stock = settings.urgent_stock, settings.show_stock
    if not tg.chat_id:
        sys.exit("TELEGRAM_CHAT_ID não definido")
    failed = 0
    try:
        tg.send_text(f"📣 <b>Novidades dos últimos {dias} dias</b>\n"
                     f"{len(alerts)} produto(s), em ordem de lançamento.")
    except TelegramError as e:
        print(f"Falhou ao enviar o cabeçalho: {e}")
    for i, a in enumerate(alerts, start=1):
        time.sleep(max(0.0, args.intervalo))  # grupos aceitam ~20 mensagens por minuto
        try:
            tg.send_alert(a)
            print(f"  [{i}/{len(alerts)}] enviado: {a.store} — {a.name[:50]}", flush=True)
        except TelegramError as e:
            failed += 1
            print(f"  [{i}/{len(alerts)}] FALHOU: {a.name[:50]} ({e})", flush=True)
    print(f"\n{len(alerts) - failed} enviado(s), {failed} falha(s).")
    return 1 if failed else 0


def cmd_detect(args) -> int:
    from .adapters import AdapterError, detect_platform
    base, _ = normalize_url(args.url)
    try:
        print(detect_platform(Fetcher(), base))
    except AdapterError as e:
        print(e)
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m monitor", description="Monitor de restock e produtos novos")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="roda o monitor (padrão)")
    for p in (parser, run_p):
        p.add_argument("--store", help="roda só as lojas cujo nome/domínio contém este texto")
        p.add_argument("--dry-run", action="store_true", help="não envia nem salva nada; só mostra os alertas")
        p.add_argument("--config", default="sites.yaml")
        p.add_argument("--state-dir", default=str(STATE_DIR))

    chat_p = sub.add_parser("chat-id", help="descobre o chat_id")
    chat_p.add_argument("--wait", type=int, default=0, help="espera até N segundos por uma mensagem")
    chat_p.add_argument("--save", action="store_true", help="grava o chat_id no .env (se houver só um)")
    sub.add_parser("test-telegram", help="envia mensagem de teste")
    rec = sub.add_parser("recentes", help="produtos que entraram nas lojas nos últimos N dias")
    rec.add_argument("--dias", type=float, default=3)
    rec.add_argument("--store", help="só lojas cujo nome/domínio contém este texto")
    rec.add_argument("--disponiveis", action="store_true", help="só produtos disponíveis")
    rec.add_argument("--enviar", action="store_true", help="envia um alerta por produto ao Telegram")
    rec.add_argument("--intervalo", type=float, default=3.5, help="segundos entre mensagens (padrão 3.5)")
    rec.add_argument("--forcar", action="store_true", help="envia mesmo se alguma loja foi lida de forma incompleta")
    det = sub.add_parser("detect", help="detecta a plataforma de uma loja")
    det.add_argument("url")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv()

    commands = {"chat-id": cmd_chat_id, "test-telegram": cmd_test_telegram, "detect": cmd_detect,
                "recentes": cmd_recentes}
    return commands.get(args.command, cmd_run)(args)


if __name__ == "__main__":
    sys.exit(main())
