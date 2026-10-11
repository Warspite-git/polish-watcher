"""Fluxo completo de várias rodadas com um adaptador falso (sem rede)."""
from datetime import datetime, timezone

import pytest

from monitor import runner, state as st
from monitor.adapters import ADAPTERS, Adapter
from monitor.config import Settings, StoreConfig, load_settings
from monitor.models import Product, ScanResult

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class FakeAdapter(Adapter):
    name = "fake"
    variants_in_listing = True
    script = []  # uma resposta por rodada: ScanResult ou Exception

    def scan(self):
        result = FakeAdapter.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def open_scan(*pairs):
    products = {pid: Product(id=pid, name=f"Esmalte {pid}", url=f"https://f.test/{pid}", available=avail,
                             stock=10 if avail else 0, price=50.0) for pid, avail in pairs}
    return ScanResult(status="open", products=products)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "fake", FakeAdapter)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    store = StoreConfig(name="Fake", url="https://f.test", key="f.test", platform="fake", delay=0)
    settings = Settings(stores=[store], error_alert_after=2)
    return settings, tmp_path


def round_(settings, state_dir):
    """Roda uma rodada e devolve os tipos de alerta gerados."""
    outcome = runner.process_store(settings.stores[0], settings, NOW, state_dir, save=True)
    return [a.kind for a in outcome.alerts]


def test_full_lifecycle(env):
    settings, state_dir = env
    FakeAdapter.script = [
        ScanResult(status="password"),              # 1: loja fechada já na 1ª execução
        open_scan(("1", True), ("2", False)),       # 2: abriu → alerta + linha de base
        open_scan(("1", True), ("2", True), ("3", True)),  # 3: restock + novo
        ScanResult(status="password"),              # 4: fechou
        RuntimeError("timeout"),                    # 5: erro
        RuntimeError("timeout"),                    # 6: erro (2ª vez → alerta)
        open_scan(("1", True), ("2", True), ("3", True)),  # 7: recuperou e reabriu
    ]
    assert round_(settings, state_dir) == []
    assert round_(settings, state_dir) == ["store_open"]
    assert st.load("f.test", state_dir)["baseline"]
    assert sorted(round_(settings, state_dir)) == ["new", "restock"]
    assert round_(settings, state_dir) == ["store_closed"]
    assert round_(settings, state_dir) == []
    assert round_(settings, state_dir) == ["store_error"]
    assert sorted(round_(settings, state_dir)) == ["store_open", "store_recovered"]


def test_alerts_go_to_outbox_without_telegram(env):
    settings, state_dir = env
    FakeAdapter.script = [open_scan(("1", False)), open_scan(("1", True))]
    runner.run(settings, state_dir=state_dir, now=NOW)
    runner.run(settings, state_dir=state_dir, now=NOW)
    pending = st.load_outbox(state_dir)
    assert [a.kind for a in pending] == ["restock"]


def test_dry_run_does_not_save(env):
    settings, state_dir = env
    FakeAdapter.script = [open_scan(("1", True))]
    runner.run(settings, dry_run=True, state_dir=state_dir, now=NOW)
    assert not (state_dir / "f.test.json").exists()


def test_state_file_roundtrip(tmp_path):
    s = st.new_state()
    s["products"] = {"10": {"name": "B", "available": True}, "9": {"name": "Ação", "available": False}}
    st.save("x.test", s, tmp_path)
    text = (tmp_path / "x.test.json").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.index('    "9": {"name": "Ação", "available": false},') < lines.index('    "10": {"name": "B", "available": true}')
    assert st.load("x.test", tmp_path) == s


def test_real_sites_yaml_loads():
    settings = load_settings("sites.yaml")
    keys = [s.key for s in settings.stores]
    assert "lojawhatcha.com.br" in keys and "drknails.com" in keys and len(keys) == 8
    assert all(s.alerts.new and s.alerts.restock for s in settings.stores)
