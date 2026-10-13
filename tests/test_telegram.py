from monitor.models import Alert
import json

from monitor.telegram import CAPTION_LIMIT, Telegram, TelegramError, brl, caption, visible_len


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
    assert "🆕 <b>NOVIDADE</b> · Loja" in text
    assert "<b>Esmalte &lt;1&gt; &amp; cia</b>" in text
    assert "R$ 65,00" in text and "🔥 Só 3 unidades. Corre!" in text
    assert text.endswith("#Loja #novidade")
    # sem botões (fallback), o link vai escapado no texto
    assert 'href="https://loja.test/produtos/1/?a=1&amp;b=2"' in caption(alert(), with_link=True)


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


# ------------------------------------------------------------ bot só no grupo configurado

class GuardTelegram(Telegram):
    def __init__(self, updates, bot_status="administrator"):
        super().__init__("T", "-100111")
        self.updates, self.bot_status, self.left, self.acked = updates, bot_status, [], None

    def call(self, method, data=None, files=None, retries=3):
        if method == "getUpdates":
            if data and "offset" in data:
                self.acked = data["offset"]
                return []
            return self.updates
        if method == "getMe":
            return {"id": 42}
        if method == "getChatMember":
            return {"status": self.bot_status}
        if method == "leaveChat":
            self.left.append(data["chat_id"])
            return True


def added(update_id, chat_id, chat_type="group", status="member"):
    return {"update_id": update_id, "my_chat_member": {
        "chat": {"id": chat_id, "type": chat_type, "title": f"chat {chat_id}"},
        "old_chat_member": {"status": "left"}, "new_chat_member": {"status": status}}}


def test_leaves_foreign_groups_but_not_channels():
    tg = GuardTelegram([added(1, -100111), added(2, -999), added(3, -100222, "channel", "administrator"),
                        added(4, 7, "private")])
    left = tg.leave_foreign_chats()
    # grupos alheios: sai. Canais: só avisa (pode ser o seu canal sendo montado)
    assert tg.left == ["-999"] and len(left) == 1
    assert tg.acked == 5  # atualizações confirmadas


def test_keeps_supergroup_migrated_from_configured_group():
    tg = GuardTelegram([added(1, -100333, "supergroup"),
                        {"update_id": 2, "message": {"chat": {"id": -100333, "type": "supergroup"},
                                                     "migrate_from_chat_id": -100111}}])
    assert tg.leave_foreign_chats() == [] and tg.left == []


def test_does_not_leave_anything_if_bot_not_in_configured_group():
    tg = GuardTelegram([added(1, -999)], bot_status="left")
    assert tg.leave_foreign_chats() == [] and tg.left == []


def test_ignores_chats_already_left():
    tg = GuardTelegram([added(1, -999), added(2, -999, status="kicked")])
    assert tg.leave_foreign_chats() == [] and tg.left == []


# ------------------------------------------------------------ formato novo

from monitor.models import Alert as A
from monitor.telegram import buttons, hashtag, tags


def test_hashtags():
    assert hashtag("By Dany Vianna") == "#ByDanyVianna"
    assert hashtag("Indie by Patty Lopes") == "#IndieByPattyLopes"
    assert hashtag("Pépper Pólish!") == "#PepperPolish"
    assert hashtag("9 Lives") == "#Loja9Lives"
    assert tags(A(kind="restock", store="Penelope Luz", store_url="")) == "#PenelopeLuz #restock"
    assert tags(A(kind="store_open", store="X", store_url="", store_tag="Patty")) == "#Patty #lojaaberta"


def test_urgency_and_stock_lines():
    one = alert(); one.stock = 1
    assert "🔥 Última unidade. Corre!" in caption(one)
    many = alert(); many.stock = 30
    assert "💰 R$ 65,00 · 📦 30 unidades" in caption(many) and "🔥" not in caption(many)
    unlimited = alert(); unlimited.stock = None
    assert "📦 disponível" in caption(unlimited)
    assert "🔥" not in caption(alert(), urgent_stock=2)  # limite configurável


def test_buttons():
    b = buttons(alert())["inline_keyboard"][0]
    assert [x["text"] for x in b] == ["🛒 Comprar agora", "🏪 Ver a loja"]
    assert b[0]["url"] == "https://loja.test/produtos/1/?a=1&b=2"
    store = A(kind="store_open", store="L", store_url="https://loja.test")
    assert buttons(store)["inline_keyboard"][0] == [{"text": "🏪 Abrir a loja", "url": "https://loja.test"}]
    assert buttons(A(kind="sold_out", store="L", store_url="https://loja.test")) is None
    bad = alert(); bad.url = "javascript:alert(1)"
    assert [x["text"] for x in buttons(bad)["inline_keyboard"][0]] == ["🏪 Ver a loja"]


def test_silent_only_for_less_urgent_alerts():
    tg = FakeTelegram()
    for kind in ("new", "restock", "store_open", "price", "store_closed"):
        a = alert(kind=kind, image=False)
        a.store_url = "https://loja.test"
        tg.send_alert(a)
    silent = [d.get("disable_notification") == "true" for _, d in tg.calls]
    assert silent == [False, False, False, True, True]


def test_sold_out_replies_to_original_message():
    tg = FakeTelegram()
    a = A(kind="sold_out", store="Whatcha", store_url="https://w.test", name="Rose Window",
          reply_to=777, note="Esgotou em menos de 15 minutos", available=False)
    tg.send_alert(a)
    method, data = tg.calls[0]
    assert method == "sendMessage" and data["disable_notification"] == "true"
    assert json.loads(data["reply_parameters"]) == {"message_id": 777, "allow_sending_without_reply": True}
    assert data["text"].startswith("⚡ <b>Esgotou em menos de 15 minutos!</b>")
    assert "#Whatcha #esgotou" in data["text"] and "reply_markup" not in data


def test_rejected_button_falls_back_to_link_in_text():
    class Picky(FakeTelegram):
        def call(self, method, data=None, files=None, retries=3):
            if data and "reply_markup" in data:
                raise TelegramError("sendPhoto: Bad Request: BUTTON_URL_INVALID")
            return super().call(method, data, files, retries)
    tg = Picky()
    tg.send_alert(alert())
    method, data = tg.calls[-1]
    assert "reply_markup" not in data and 'href="https://loja.test/produtos/1/' in data["caption"]


def test_message_ids_are_recorded():
    class Ids(FakeTelegram):
        n = 100
        def call(self, method, data=None, files=None, retries=3):
            super().call(method, data, files, retries)
            if method == "sendMediaGroup":
                return [{"message_id": i} for i in range(1, len(json.loads(data["media"])) + 1)]
            Ids.n += 1
            return {"message_id": Ids.n}
    tg = Ids()
    single = alert()
    tg.send_alert(single)
    assert single.message_id == 101
    group = [alert(i) for i in range(3)]
    tg.send_album(group, "h")
    assert [a.message_id for a in group] == [1, 2, 3]


def test_replenish_caption():
    a = alert(kind="replenish")
    a.old_stock, a.stock = 3, 30
    text = caption(a)
    assert text.startswith("📦 <b>REPOSIÇÃO</b> · Loja")
    assert "💰 R$ 65,00 · 📦 3 → 30 unidades (+27)" in text
    assert text.endswith("#Loja #reposicao")
    assert [b["text"] for b in buttons(a)["inline_keyboard"][0]] == ["🛒 Comprar", "🏪 Ver a loja"]


def test_zero_price_is_possible_freebie():
    a = alert(); a.price = 0.0; a.stock = 50
    text = caption(a)
    assert "🎁 R$ 0,00 (possível brinde) · 📦 50 unidades" in text and "grátis" not in text
    assert text.endswith("#Loja #novidade #brinde")
    from monitor.telegram import compact_line
    assert "🎁 possível brinde" in compact_line(a)


def test_store_with_several_hashtags():
    a = A(kind="new", store="Nothing Basic & Phoenix", store_url="https://nb.test", name="X", url="https://nb.test/p",
          price=50.0, stock=10, store_tag="NothingBasic Phoenix")
    text = caption(a)
    assert "🆕 <b>NOVIDADE</b> · Nothing Basic &amp; Phoenix" in text
    assert text.endswith("#NothingBasic #Phoenix #novidade")
    opened = A(kind="store_open", store="Nothing Basic & Phoenix", store_url="https://nb.test", store_tag="NothingBasic Phoenix")
    assert caption(opened).endswith("#NothingBasic #Phoenix #lojaaberta")


def test_hide_stock_counts():
    from monitor.telegram import compact_line
    a = alert(); a.stock = 30
    assert "📦 disponível" in caption(a, show_stock=False) and "30" not in caption(a, show_stock=False)
    low = alert(); low.stock = 3
    text = caption(low, show_stock=False)
    assert "🔥 Últimas unidades. Corre!" in text and "3 unidades" not in text
    rep = alert(kind="replenish"); rep.old_stock, rep.stock = 3, 30
    text = caption(rep, show_stock=False)
    assert "📦 Chegaram mais unidades" in text and "30" not in text
    assert "un." not in compact_line(a, show_stock=False)
    assert "🔥 últimas unidades" in compact_line(low, show_stock=False)
    tg = FakeTelegram(); tg.show_stock = False
    tg.send_alert(low)
    assert "3 unidades" not in tg.calls[0][1]["caption"]
