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
    assert keys, "o sites.yaml precisa ter pelo menos uma loja"
    assert len(keys) == len(set(keys))  # sem lojas repetidas
    assert all(s.url.startswith("https://") for s in settings.stores)


def test_collect_recent_orders_by_date_and_filters(env):
    from monitor.recent import collect_recent
    settings, _ = env
    def p(pid, added, avail=True, exact=False):
        return Product(id=pid, name=f"Esmalte {pid}", url=f"https://f.test/{pid}", available=avail,
                       added_at=added, added_at_exact=exact)
    FakeAdapter.script = [ScanResult(status="open", products={x.id: x for x in [
        p("velho", "2026-10-01T12:00:00+00:00"),
        p("b", "2026-10-10T15:00:00+00:00", exact=True),
        p("a", "2026-10-09T12:00:00+00:00"),
        p("esgotado", "2026-10-10T16:00:00+00:00", avail=False),
        p("sem-data", None),
    ]})]
    alerts = collect_recent(settings, 3, now=NOW)
    assert [a.name for a in alerts] == ["Esmalte a", "Esmalte b", "Esmalte esgotado"]
    assert alerts[0].note == "🗓️ Adicionado em 09/10 (aprox.)"
    assert alerts[1].note == "🗓️ Adicionado em 10/10 às 12:00"  # horário de Brasília
    FakeAdapter.script = [ScanResult(status="open", products={"x": p("x", "2026-10-10T16:00:00+00:00", avail=False)})]
    assert collect_recent(settings, 3, now=NOW, available_only=True) == []



def test_announced_products_remember_message_and_get_sold_out_reply(env, monkeypatch):
    """Rodada 1: restock enviado (mensagem 555). Rodada 2: esgotou → resposta à mensagem 555."""
    from datetime import timedelta
    import monitor.telegram as tgmod
    settings, state_dir = env
    sent = []

    def fake_call(self, method, data=None, files=None, retries=3):
        sent.append((method, dict(data or {})))
        return [] if method == "getUpdates" else {"message_id": 555}

    monkeypatch.setattr(tgmod.Telegram, "call", fake_call)
    monkeypatch.setenv("TELEGRAM_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    FakeAdapter.script = [open_scan(("1", False)), open_scan(("1", True)), open_scan(("1", False))]
    runner.run(settings, state_dir=state_dir, now=NOW)                          # linha de base
    runner.run(settings, state_dir=state_dir, now=NOW)                          # restock
    assert st.load("f.test", state_dir)["products"]["1"]["alert"]["msg"] == 555
    runner.run(settings, state_dir=state_dir, now=NOW + timedelta(minutes=14))  # esgotou
    method, data = sent[-1]
    assert method == "sendMessage" and "Esgotou em menos de 15 minutos" in data["text"]
    assert '"message_id": 555' in data["reply_parameters"]
    assert "alert" not in st.load("f.test", state_dir)["products"]["1"]
