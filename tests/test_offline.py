"""Офлайн-тест (без Битрикс24): нормализация телефонов, правило против дублей в КЛАССИЧЕСКОМ режиме
(портал для живого теста — в простом), очередь заявок при недоступной CRM.

    python tests/test_offline.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.update(OUTBOX_PATH=str(Path(tempfile.mkdtemp()) / "outbox.db"),
                  B24_WEBHOOK="https://test.bitrix24.ru/rest/1/x/", BOT_TOKEN="123:TEST", ADMIN_ID="1")
sys.path.insert(0, str(Path(__file__).parent.parent))

import crm  # noqa: E402
import outbox  # noqa: E402
from b24 import B24, B24Error  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


class FakeB24(B24):
    """Битрикс24 в памяти, классический режим (лиды). down=True — имитация недоступной CRM."""

    def __init__(self):
        super().__init__("https://test.bitrix24.ru/rest/1/x/")
        self.leads, self.contacts, self.comments, self.down = {}, {}, [], False

    async def call(self, method, params=None):
        if self.down:
            raise B24Error("INTERNAL_SERVER_ERROR", "технические работы")
        p = params or {}
        if method == "crm.settings.mode.get":
            return 1
        if method == "crm.duplicate.findbycomm":
            phone = p["values"][0]
            res = {k: [i for i, e in store.items() if phone in [x["VALUE"] for x in e.get("PHONE", [])]]
                   for k, store in (("LEAD", self.leads), ("CONTACT", self.contacts))}
            res = {k: v for k, v in res.items() if v}
            return res or []  # как настоящий Битрикс: [] вместо {}
        if method == "crm.lead.get":
            return self.leads[p["id"]]
        if method == "crm.lead.add":
            i = len(self.leads) + 1
            self.leads[i] = {**p["fields"], "STATUS_SEMANTIC_ID": "P"}
            return i
        if method == "crm.timeline.comment.add":
            self.comments.append(p["fields"])
            return 1
        raise AssertionError(method)


async def main():
    # --- телефоны ---
    for raw, exp in [("8 (913) 000-11-22", "+79130001122"), ("+7 913 0001122", "+79130001122"),
                     ("9130001122", "+79130001122"), ("7-913-000-11-22", "+79130001122"),
                     ("+375 29 123-45-67", "+375291234567"), ("12345", None), ("", None), ("8 800", None)]:
        check(f"телефон «{raw}» → {exp}", crm.normalize_phone(raw) == exp)

    # --- классический режим ---
    b = FakeB24()
    r1 = await crm.submit(b, name="Анна", phone="+79130001122", need="Бот для записи")
    check("классический: новый телефон → новый лид", r1.action == "created" and r1.entity == "lead")
    r2 = await crm.submit(b, name="Анна", phone="+79130001122", need="Ещё рассылка")
    check("классический: лид в работе → комментарий, без нового лида",
          r2.action == "attached" and r2.entity_id == r1.entity_id and len(b.leads) == 1 and b.comments)
    b.leads[r1.entity_id]["STATUS_SEMANTIC_ID"] = "F"
    b.contacts[7] = {"PHONE": [{"VALUE": "+79130001122"}]}
    r3 = await crm.submit(b, name="Анна", phone="+79130001122", need="Вернулась")
    check("классический: лид закрыт, есть контакт → «Повторное обращение» с привязкой к контакту",
          r3.action == "repeat" and b.leads[r3.entity_id]["CONTACT_ID"] == 7
          and b.leads[r3.entity_id]["TITLE"].startswith("Повторное"))

    # --- очередь при недоступной CRM (через функции бота) ---
    import bot
    sent = []

    async def fake_send(chat_id, text, **kw):
        sent.append(text)
    bot.bot.send_message = fake_send
    fake = FakeB24()
    bot.b24 = fake
    outbox.init()
    fake.down = True
    payload = {"name": "Олег", "phone": "+79001112233", "need": "Парсер цен", "tg_username": None}
    try:
        await crm.submit(fake, **payload)
    except bot.CRM_DOWN as e:
        outbox.put(payload, repr(e))
    check("CRM недоступна → заявка сохранена в очередь", len(outbox.pending()) == 1)
    await bot.retry_outbox()
    check("CRM всё ещё лежит → заявка остаётся в очереди, попытка учтена",
          len(outbox.pending()) == 1 and outbox.pending()[0]["attempts"] == 2)
    fake.down = False
    await bot.retry_outbox()
    check("CRM вернулась → заявка доставлена, лид создан, админ уведомлён",
          not outbox.pending() and len(fake.leads) == 1 and any("из очереди доставлена" in t for t in sent))
    await bot.retry_outbox()
    check("повторный проход очереди — без повторной отправки", len(fake.leads) == 1)
    await bot.bot.session.close()

    print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ОШИБКИ")
    sys.exit(0 if ok else 1)


asyncio.run(main())
