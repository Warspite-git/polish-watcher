"""Grupo privado: lojas privadas só lá, nada se mistura no grupo público, dados cifrados."""
import json
import logging
from datetime import datetime, timezone

import pytest

from monitor import runner, state as st
from monitor.adapters import ADAPTERS, Adapter
from monitor.config import Settings, StoreConfig, load_settings
from monitor.models import Product, ScanResult

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


class Scripted(Adapter):
    name = "scripted"
    variants_in_listing = True
    scripts = {}

    def scan(self):
        return Scripted.scripts[self.store.key].pop(0)


def scan_of(*items):
    return ScanResult(status="open", products={
        pid: Product(id=pid, name=f"{name}", url=f"https://x.test/{pid}", available=avail, stock=stock,
                     price=60.0, image=f"https://img.test/{pid}.jpg") for pid, name, avail, stock in items})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(ADAPTERS, "scripted", Scripted)
    monkeypatch.setenv("STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELEGRAM_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100PUBLICO")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRIVADO", "-555PRIVADO")
    public = StoreConfig(name="Loja Pública", url="https://publica.test", key="publica.test", platform="scripted",
                         delay=0, logo="https://img.test/logo.png")
    secret = StoreConfig(name="Loja Secreta", url="https://www.secreta.test", key="secreta.test", platform="scripted",
                         delay=0, logo="https://img.test/logo.png", private=True, alias="loja privada 1")
    settings = Settings(stores=[public, secret], show_stock=False, show_stock_private=True)
    sent = []

    def fake_call(self, method, data=None, files=None, retries=3):
        sent.append((self.chat_id, method, dict(data or {})))
        return [] if method == "getUpdates" else {"message_id": len(sent), "photo": [{}]}

    import monitor.telegram as tgmod
    monkeypatch.setattr(tgmod.Telegram, "call", fake_call)
    return settings, tmp_path, sent


def run_round(settings, state_dir, pub, sec):
    Scripted.scripts = {"publica.test": [pub], "secreta.test": [sec]}
    runner.run(settings, state_dir=state_dir, now=NOW)


def posts(sent, chat):
    return [d.get("caption") or d.get("text") or "" for c, m, d in sent if c == chat and m.startswith("send")]


def test_private_store_never_reaches_public_group(setup):
    settings, state_dir, sent = setup
    run_round(settings, state_dir, scan_of(("1", "Esmalte A", False, 0)), scan_of(("9", "Esmalte Secreto", False, 0)))
    run_round(settings, state_dir, scan_of(("1", "Esmalte A", True, 30)), scan_of(("9", "Esmalte Secreto", True, 12)))
    public, private = posts(sent, "-100PUBLICO"), posts(sent, "-555PRIVADO")
    # grupo público: só a loja pública; nada da loja secreta
    assert len(public) == 1 and "Esmalte A" in public[0]
    assert not any("Secret" in p for p in public)
    # grupo privado: as duas lojas
    assert len(private) == 2
    assert any("Esmalte A" in p for p in private) and any("Esmalte Secreto" in p for p in private)


def test_stock_only_shown_in_private_group(setup):
    settings, state_dir, sent = setup
    run_round(settings, state_dir, scan_of(("1", "Esmalte A", False, 0)), scan_of(("9", "S", False, 0)))
    run_round(settings, state_dir, scan_of(("1", "Esmalte A", True, 30)), scan_of(("9", "S", False, 0)))
    (public,), private = posts(sent, "-100PUBLICO"), posts(sent, "-555PRIVADO")
    assert "📦 disponível" in public and "30 unidades" not in public
    assert "📦 30 unidades" in private[0]


def test_private_replenish_shows_units_added(setup):
    settings, state_dir, sent = setup
    settings.stores[1].alerts.replenish = 5
    run_round(settings, state_dir, scan_of(("1", "A", True, 3)), scan_of(("9", "Esmalte Secreto", True, 3)))
    run_round(settings, state_dir, scan_of(("1", "A", True, 3)), scan_of(("9", "Esmalte Secreto", True, 30)))
    (rep,) = posts(sent, "-555PRIVADO")
    assert "📦 3 → 30 unidades (+27)" in rep and posts(sent, "-100PUBLICO") == []


def test_private_state_is_encrypted_and_name_hidden(setup):
    settings, state_dir, _ = setup
    run_round(settings, state_dir, scan_of(("1", "A", True, 3)), scan_of(("9", "Esmalte Secreto", True, 3)))
    files = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    assert "publica.test.json" in files
    assert not any("secreta" in n for n in files)                 # nome do arquivo não revela a loja
    private_files = [b for n, b in files.items() if n.startswith("p-")]
    assert len(private_files) == 1 and b"Secreto" not in private_files[0]   # conteúdo cifrado
    assert st.load("secreta.test", state_dir, private=True)["products"]["9"]["name"] == "Esmalte Secreto"


def test_private_state_not_rewritten_when_unchanged(setup):
    settings, state_dir, _ = setup
    run_round(settings, state_dir, scan_of(("1", "A", True, 3)), scan_of(("9", "S", True, 3)))
    (path,) = [p for p in state_dir.iterdir() if p.name.startswith("p-")]
    before = path.read_bytes()
    run_round(settings, state_dir, scan_of(("1", "A", True, 3)), scan_of(("9", "S", True, 3)))
    assert path.read_bytes() == before  # sem commit à toa no repositório


def test_public_logs_never_name_private_store(setup, caplog, monkeypatch, tmp_path):
    settings, state_dir, _ = setup
    summary = tmp_path / "summary.md"
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with caplog.at_level(logging.INFO):
        run_round(settings, state_dir, scan_of(("1", "A", False, 0)), scan_of(("9", "Esmalte Secreto", False, 0)))
        run_round(settings, state_dir, scan_of(("1", "A", False, 0)), scan_of(("9", "Esmalte Secreto", True, 5)))
    public_text = caplog.text + summary.read_text() + output.read_text()
    for secret in ("Secreta", "secreta.test", "Esmalte Secreto"):
        assert secret not in public_text
    assert "loja privada 1" in public_text
    assert "sem alertas" in output.read_text()  # o commit público não conta alertas da loja privada


def test_store_log_masks_domain_in_errors():
    store = StoreConfig(name="Loja Secreta", url="https://www.secreta.test", key="secreta.test", private=True,
                        alias="loja privada 1")
    assert runner._mask(store, "HTTP 404 em https://www.secreta.test/produtos/x") == \
        "HTTP 404 em https://loja privada 1/produtos/x"


def test_private_stores_loaded_from_secret_not_sites_yaml():
    yaml_text = "[{nome: Loja Secreta, url: https://www.secreta.test/}]"
    s = load_settings("sites.yaml", private_yaml=yaml_text)
    secret = [x for x in s.stores if x.private]
    assert [(x.name, x.display) for x in secret] == [("Loja Secreta", "loja privada 1")]
    assert not any(x.private for x in load_settings("sites.yaml", private_yaml="").stores)


def test_store_listed_in_both_places_counts_as_private(tmp_path):
    """Durante a troca (segredo já atualizado, sites.yaml ainda não) a rodada não pode falhar."""
    from monitor.config import load_settings
    cfg = tmp_path / "sites.yaml"
    cfg.write_text("lojas:\n  - nome: Aberta\n    url: https://aberta.test/\n  - nome: Movida\n    url: https://www.movida.test/\n")
    settings = load_settings(str(cfg), private_yaml='[{nome: Movida, url: "https://movida.test"}]')
    assert [(s.key, s.private) for s in settings.stores] == [("aberta.test", False), ("movida.test", True)]


def test_store_turned_private_migrates_its_open_state(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from monitor import state as st
    monkeypatch.setenv("STATE_KEY", Fernet.generate_key().decode())
    saved = st.new_state()
    saved["baseline"] = "2026-10-11T00:00:00+00:00"
    saved["products"] = {"1": {"name": "Esmalte", "alert": {"at": "x", "msgs": {"privado": 9}}}}
    st.save("movida.test", saved, tmp_path)
    loaded = st.load("movida.test", tmp_path, private=True)  # sem arquivo cifrado ainda: lê o aberto
    assert loaded == saved
    st.save("movida.test", loaded, tmp_path, private=True)
    assert not (tmp_path / "movida.test.json").exists()
    assert [f.suffix for f in tmp_path.iterdir()] == [".bin"]
    assert b"Esmalte" not in next(tmp_path.iterdir()).read_bytes()
    assert st.load("movida.test", tmp_path, private=True) == saved
