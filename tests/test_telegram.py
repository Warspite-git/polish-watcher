from monitor.models import Alert
from monitor.telegram import CAPTION_LIMIT, Telegram, brl, caption, visible_len


def alert(i=1, kind="new", image=True, store="Loja"):
    return Alert(kind=kind, store=store, store_url="https://loja.test", name=f"Esmalte <{i}> & cia",
                 url=f"https://loja.test/produtos/{i}/?a=1&b=2",
                 image=f"https://img.test/{i}.jpg" if image else None, price=65.0, stock=3)


def test_brl():
    assert brl(65) == "R$ 65,00"
    assert brl(1234.5) == "R$ 1.234,50"
    assert brl(None) == "—"


def test_caption_escapes_html():
    text = caption(alert())
    assert "🆕 <b>Produto novo</b> · Loja" in text
    assert "<b>Esmalte &lt;1&gt; &amp; cia</b>" in text
    assert 'href="https://loja.test/produtos/1/?a=1&amp;b=2"' in text
    assert "R$ 65,00" in text and "3 unidades" in text


def test_price_caption():
    a = alert(kind="price")
    a.old_price = 70
    assert "<s>R$ 70,00</s> → <b>R$ 65,00</b>" in caption(a)


class FakeTelegram(Telegram):
    def __init__(self):
        super().__init__("TOKEN", "123")
        self.calls = []

    def call(self, method, data=None, files=None, retries=3):
        self.calls.append((method, data))
        return {}


def test_few_alerts_go_individually_with_photo():
    tg = FakeTelegram()
    failed = tg.send_alerts([alert(i) for i in range(3)], max_individual=10)
    assert failed == []
    assert [m for m, _ in tg.calls] == ["sendPhoto"] * 3


def test_many_alerts_are_grouped_in_albums():
    tg = FakeTelegram()
    alerts = [alert(i) for i in range(23)] + [alert(99, image=False)]
    tg.send_alerts(alerts, max_individual=10)
    methods = [m for m, _ in tg.calls]
    # 23 com foto → álbuns de 10 + 10 + 3; o sem foto vai no resumo em texto
    assert methods == ["sendMediaGroup", "sendMediaGroup", "sendMediaGroup", "sendMessage"]
    import json
    media = json.loads(tg.calls[0][1]["media"])
    assert len(media) == 10 and visible_len(media[0]["caption"]) <= CAPTION_LIMIT
    assert "(1/3)" in media[0]["caption"]


def test_store_alerts_are_text():
    tg = FakeTelegram()
    a = Alert(kind="store_open", store="Nothing Basic", store_url="https://nb.test", note="reabriu")
    tg.send_alerts([a])
    assert tg.calls[0][0] == "sendMessage"
    assert "🔓" in tg.calls[0][1]["text"]


def test_token_is_redacted():
    tg = Telegram("123:SECRET", "1")
    assert "SECRET" not in tg.redact("erro em https://api.telegram.org/bot123:SECRET/sendPhoto")


def test_follows_supergroup_migration(monkeypatch):
    import monitor.telegram as tgmod

    class R:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    sent = []

    def fake_post(url, data=None, files=None, timeout=None):
        sent.append(data["chat_id"])
        if data["chat_id"] == "-555":
            return R(400, {"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                           "parameters": {"migrate_to_chat_id": -100555}})
        return R(200, {"ok": True, "result": {}})

    monkeypatch.setattr(tgmod.requests, "post", fake_post)
    monkeypatch.setattr(tgmod, "MIN_INTERVAL", 0)
    tg = Telegram("T", "-555")
    tg.send_text("oi")
    assert sent == ["-555", "-100555"] and tg.chat_id == "-100555"
