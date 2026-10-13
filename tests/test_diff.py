from datetime import datetime, timedelta, timezone

from monitor.config import AlertConfig, StoreConfig
from monitor.diff import baseline, diff_products, select_variant_fetches
from monitor.models import Product, ScanResult, Variant

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def store(**alerts):
    return StoreConfig(name="Loja", url="https://loja.test", key="loja.test", alerts=AlertConfig(**alerts))


def prod(pid, available=True, stock=10, price=50.0, name=None, variants=None):
    return Product(id=pid, name=name or f"Esmalte {pid}", url=f"https://loja.test/produtos/{pid}/",
                   image=f"https://img.test/{pid}.jpg", price=price, available=available, stock=stock,
                   variants=variants)


def scan(*products, complete=True):
    return ScanResult(status="open", products={p.id: p for p in products}, complete=complete)


def kinds(alerts):
    return sorted(a.kind for a in alerts)


def test_baseline_has_no_alerts_and_records_everything():
    state = baseline(scan(prod("1"), prod("2", available=False, stock=0)), NOW)
    assert set(state) == {"1", "2"}
    assert state["2"]["available"] is False and state["1"]["first_seen"] == "2026-10-11"


def test_new_product_and_restock():
    old = baseline(scan(prod("1"), prod("2", available=False, stock=0)), NOW)
    alerts, new_state = diff_products(store(), old, scan(prod("1"), prod("2", stock=4), prod("3")), NOW)
    assert kinds(alerts) == ["new", "restock"]
    restock = next(a for a in alerts if a.kind == "restock")
    assert restock.name == "Esmalte 2" and restock.stock == 4
    assert new_state["2"]["available"] is True


def test_no_alert_when_nothing_changes_or_stock_just_drops():
    old = baseline(scan(prod("1", stock=10)), NOW)
    alerts, _ = diff_products(store(), old, scan(prod("1", stock=7)), NOW)
    assert alerts == []


def test_low_stock_only_when_crossing_threshold():
    old = baseline(scan(prod("1", stock=10)), NOW)
    alerts, state = diff_products(store(low_stock=3), old, scan(prod("1", stock=3)), NOW)
    assert kinds(alerts) == ["low_stock"]
    alerts, _ = diff_products(store(low_stock=3), state, scan(prod("1", stock=2)), NOW)
    assert alerts == []  # já estava abaixo do limite


def test_price_change_is_optional():
    old = baseline(scan(prod("1", price=50)), NOW)
    alerts, _ = diff_products(store(), old, scan(prod("1", price=45)), NOW)
    assert alerts == []
    alerts, _ = diff_products(store(price_change=True), old, scan(prod("1", price=45)), NOW)
    assert kinds(alerts) == ["price"] and alerts[0].old_price == 50 and alerts[0].price == 45


def test_filters():
    old = baseline(scan(prod("1")), NOW)
    s = store()
    s.include, s.exclude = ["magnético"], ["base"]
    current = scan(prod("1"), prod("2", name="Esmalte Magnetico Azul"), prod("3", name="Base Magnética"),
                   prod("4", name="Holo Rosa"))
    alerts, state = diff_products(s, old, current, NOW)
    assert [a.name for a in alerts] == ["Esmalte Magnetico Azul"]
    assert set(state) == {"1", "2", "3", "4"}  # tudo é registrado, só o alerta é filtrado


def test_variant_restock():
    v_old = {"10": Variant("10", "Mini", False, 0), "11": Variant("11", "Full", True, 5)}
    v_new = {"10": Variant("10", "Mini", True, 3), "11": Variant("11", "Full", True, 4)}
    old = baseline(scan(prod("1", stock=5, variants=v_old)), NOW)
    alerts, _ = diff_products(store(), old, scan(prod("1", stock=7, variants=v_new)), NOW)
    assert kinds(alerts) == ["variant_restock"]
    assert alerts[0].variants == ["Mini (3)"]


def test_restock_lists_available_variants():
    old = baseline(scan(prod("1", available=False, stock=0)), NOW)
    v = {"10": Variant("10", "Mini", True, 2), "11": Variant("11", "Full", False, 0)}
    alerts, state = diff_products(store(), old, scan(prod("1", stock=2, variants=v)), NOW, variants_from_page={"1"})
    assert alerts[0].kind == "restock" and alerts[0].variants == ["Mini (2)"]
    assert state["1"]["nv"] == 2 and "variants_checked" in state["1"]


def test_missing_products_become_unavailable_and_reappear_as_restock():
    old = baseline(scan(prod("1"), prod("2")), NOW)
    # 1ª varredura sem o produto 2: ainda não conta como indisponível
    alerts, state = diff_products(store(), old, scan(prod("1")), NOW)
    assert alerts == [] and state["2"]["missing"] == 1 and state["2"]["available"] is True
    # 2ª varredura sem ele: loja provavelmente escondeu por estar esgotado
    alerts, state = diff_products(store(), state, scan(prod("1")), NOW)
    assert state["2"]["missing"] == 2 and state["2"]["available"] is False
    # reapareceu disponível → restock
    alerts, state = diff_products(store(), state, scan(prod("1"), prod("2")), NOW)
    assert kinds(alerts) == ["restock"] and "missing" not in state["2"]


def test_incomplete_scan_does_not_mark_missing():
    old = baseline(scan(prod("1"), prod("2")), NOW)
    _, state = diff_products(store(), old, scan(prod("1"), complete=False), NOW)
    assert "missing" not in state["2"]


def test_forget_long_gone_products():
    old = baseline(scan(prod("1"), prod("2")), NOW)
    old["2"].update(missing=2, missing_since="2026-01-01", available=False)
    _, state = diff_products(store(), old, scan(prod("1")), NOW)
    assert "2" not in state


def test_select_variant_fetches():
    old = baseline(scan(prod("1", stock=5), prod("2", available=False, stock=0), prod("3", stock=5),
                        prod("4", stock=5)), NOW)
    old["3"]["nv"] = 1  # sabemos que tem uma variante só
    old["4"].update(nv=2, variants={}, variants_checked=(NOW - timedelta(hours=7)).isoformat())
    current = scan(prod("1", stock=9), prod("2", stock=3), prod("3", stock=9), prod("4", stock=5), prod("5"))
    assert select_variant_fetches(old, current, cap=10, now=NOW) == ["2", "1", "5", "4"]
    assert select_variant_fetches(old, current, cap=2, now=NOW) == ["2", "1"]


# ------------------------------------------------------------ "esgotou"

from monitor.diff import sold_out_note


def test_sold_out_note():
    at = (NOW - timedelta(minutes=12)).isoformat()
    assert sold_out_note({"msg": 5, "at": at}, NOW) == "Esgotou em menos de 15 minutos"
    assert sold_out_note({"msg": 5, "at": (NOW - timedelta(minutes=2)).isoformat()}, NOW) == "Esgotou em menos de 5 minutos"
    assert sold_out_note({"msg": 5, "at": (NOW - timedelta(minutes=58)).isoformat()}, NOW) == "Esgotou em menos de 1 hora"
    assert sold_out_note({"msg": 5, "at": (NOW - timedelta(hours=5, minutes=1)).isoformat()}, NOW) == "Esgotou em menos de 6 horas"
    assert sold_out_note({"msg": 5, "at": (NOW - timedelta(days=3)).isoformat()}, NOW) is None  # antigo demais
    assert sold_out_note(None, NOW) is None


def test_sold_out_reply_only_for_announced_products():
    old = baseline(scan(prod("1"), prod("2")), NOW)
    old["1"]["alert"] = {"msg": 42, "at": (NOW - timedelta(minutes=20)).isoformat()}
    alerts, state = diff_products(store(), old, scan(prod("1", available=False, stock=0),
                                                     prod("2", available=False, stock=0)), NOW)
    assert [(a.kind, a.reply_to, a.note) for a in alerts] == [("sold_out", 42, "Esgotou em menos de 20 minutos")]
    assert "alert" not in state["1"]  # não responde duas vezes
    assert alerts[0].store_key == "loja.test" and alerts[0].product_id == "1"


def test_alert_record_survives_while_available():
    old = baseline(scan(prod("1")), NOW)
    old["1"]["alert"] = {"msg": 42, "at": NOW.isoformat()}
    _, state = diff_products(store(), old, scan(prod("1", stock=3)), NOW)
    assert state["1"]["alert"]["msg"] == 42


def test_sold_out_can_be_disabled():
    old = baseline(scan(prod("1")), NOW)
    old["1"]["alert"] = {"msg": 42, "at": NOW.isoformat()}
    alerts, _ = diff_products(store(sold_out=False), old, scan(prod("1", available=False, stock=0)), NOW)
    assert alerts == []


def test_hidden_product_counts_as_sold_out():
    old = baseline(scan(prod("1"), prod("2")), NOW)
    old["2"]["alert"] = {"msg": 9, "at": (NOW - timedelta(minutes=30)).isoformat()}
    _, state = diff_products(store(), old, scan(prod("1")), NOW)          # sumiu 1 vez
    alerts, state = diff_products(store(), state, scan(prod("1")), NOW)  # sumiu 2 vezes
    assert [(a.kind, a.reply_to, a.name) for a in alerts] == [("sold_out", 9, "Esmalte 2")]
    assert "alert" not in state["2"] and state["2"]["available"] is False


# ------------------------------------------------------------ reposição

def test_replenish_when_stock_grows_without_selling_out():
    old = baseline(scan(prod("1", stock=3)), NOW)
    alerts, _ = diff_products(store(replenish=5), old, scan(prod("1", stock=30)), NOW)
    assert [(a.kind, a.old_stock, a.stock) for a in alerts] == [("replenish", 3, 30)]


def test_replenish_respects_minimum_and_switch():
    old = baseline(scan(prod("1", stock=3)), NOW)
    assert diff_products(store(replenish=5), old, scan(prod("1", stock=7)), NOW)[0] == []   # +4 < 5
    assert diff_products(store(replenish=5), old, scan(prod("1", stock=8)), NOW)[0][0].kind == "replenish"  # +5
    assert diff_products(store(), old, scan(prod("1", stock=30)), NOW)[0] == []  # desligado


def test_sold_out_then_back_is_restock_not_replenish():
    old = baseline(scan(prod("1", available=False, stock=0)), NOW)
    alerts, _ = diff_products(store(replenish=5), old, scan(prod("1", stock=30)), NOW)
    assert [a.kind for a in alerts] == ["restock"]


def test_no_replenish_without_stock_count_or_with_variant_restock():
    old = baseline(scan(prod("1", stock=None)), NOW)
    assert diff_products(store(replenish=5), old, scan(prod("1", stock=None)), NOW)[0] == []
    v_old = {"10": Variant("10", "Mini", False, 0), "11": Variant("11", "Full", True, 5)}
    v_new = {"10": Variant("10", "Mini", True, 20), "11": Variant("11", "Full", True, 5)}
    old = baseline(scan(prod("2", stock=5, variants=v_old)), NOW)
    alerts, _ = diff_products(store(replenish=5), old, scan(prod("2", stock=25, variants=v_new)), NOW)
    assert [a.kind for a in alerts] == ["variant_restock"]  # não duplica com reposição


# ------------------------------------------------------------ produto novo sem foto

def nophoto(pid, **kw):
    p = prod(pid, **kw)
    p.image = None
    return p


def test_new_product_without_photo_waits_then_announces_with_photo():
    old = baseline(scan(prod("1")), NOW)
    alerts, state = diff_products(store(), old, scan(prod("1"), nophoto("2")), NOW)
    assert alerts == [] and state["2"]["photo_wait"] == 1
    alerts, state = diff_products(store(), state, scan(prod("1"), prod("2")), NOW)   # a foto subiu
    assert [(a.kind, a.image) for a in alerts] == [("new", "https://img.test/2.jpg")]
    assert "photo_wait" not in state["2"]


def test_new_product_without_photo_is_announced_after_waiting():
    old = baseline(scan(prod("1")), NOW)
    _, state = diff_products(store(), old, scan(prod("1"), nophoto("2")), NOW)
    alerts, state = diff_products(store(), state, scan(prod("1"), nophoto("2")), NOW)
    assert alerts == [] and state["2"]["photo_wait"] == 2
    alerts, state = diff_products(store(), state, scan(prod("1"), nophoto("2")), NOW)
    assert [a.kind for a in alerts] == ["new"]  # vai sem foto (o runner põe o logo da loja)


def test_product_removed_while_waiting_for_photo_is_never_posted():
    old = baseline(scan(prod("1")), NOW)
    _, state = diff_products(store(), old, scan(prod("1"), nophoto("2")), NOW)
    for _ in range(3):
        alerts, state = diff_products(store(), state, scan(prod("1")), NOW)
        assert alerts == []
