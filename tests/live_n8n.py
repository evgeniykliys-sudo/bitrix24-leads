"""Живая проверка n8n-сценария «Форма сайта → Битрикс24 без дублей» на настоящем портале.
Шлёт заявки в вебхук n8n и проверяет, что оказалось в CRM; созданное удаляет.

    python tests/live_n8n.py
"""
import asyncio
import json
import os
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")
from b24 import B24  # noqa: E402

N8N = "http://localhost:5678/webhook/lead-to-b24"
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


def post(body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(N8N, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")  # пустое тело = сценарий упал, не дойдя до ответа
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


async def cleanup(b, phone):
    for c in await b.call("crm.contact.list", {"filter": {"PHONE": phone}, "select": ["ID"]}):
        for d in await b.call("crm.deal.list", {"filter": {"CONTACT_ID": c["ID"]}, "select": ["ID"]}):
            await b.call("crm.deal.delete", {"id": d["ID"]})
        await b.call("crm.contact.delete", {"id": c["ID"]})


async def main():
    b = B24(os.environ["B24_WEBHOOK"])
    digits = f"900{random.randint(1000000, 9999999)}"
    phone = "+7" + digits

    code, r = post({"name": "[тест n8n] Ирина", "phone": "12345", "need": "x"})
    check(f"0. неверный телефон → 422 ({r.get('error')})", code == 422)

    try:
        code, r1 = post({"name": "[тест n8n] Ирина", "phone": phone, "need": "Нужен сайт-визитка"})
        check(f"1. новый клиент → новая сделка ({code}, {r1})", code == 200 and r1.get("status") == "created")

        code, r2 = post({"name": "[тест n8n] Ирина", "phone": f"8 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}",
                         "need": "И ещё чат-бот"})
        check("2. тот же номер в другом формате, сделка открыта → комментарий, без дубля",
              code == 200 and r2.get("status") == "attached" and r2.get("deal_id") == r1.get("deal_id"))
        comments = await b.call("crm.timeline.comment.list", {"filter": {"ENTITY_ID": r1["deal_id"], "ENTITY_TYPE": "deal"}})
        check("   комментарий в ленте сделки", any("чат-бот" in c["COMMENT"] for c in comments))
        contacts = await b.call("crm.contact.list", {"filter": {"PHONE": phone}, "select": ["ID"]})
        deals = await b.call("crm.deal.list", {"filter": {"CONTACT_ID": contacts[0]["ID"]}, "select": ["ID"]}) if contacts else []
        check(f"   один контакт, одна сделка (контактов {len(contacts)}, сделок {len(deals)})", len(contacts) == 1 and len(deals) == 1)

        await b.call("crm.deal.update", {"id": r1["deal_id"], "fields": {"STAGE_ID": "LOSE"}})
        code, r3 = post({"name": "[тест n8n] Ирина", "phone": phone, "need": "Вернулась через месяц"})
        deal3 = await b.call("crm.deal.get", {"id": r3.get("deal_id")}) if r3.get("deal_id") else {}
        check("3. прошлая сделка закрыта → «Повторное обращение» к тому же контакту, источник «Веб-сайт»",
              r3.get("status") == "repeat" and str(deal3.get("CONTACT_ID")) == str(contacts[0]["ID"])
              and deal3.get("SOURCE_ID") == "WEB" and deal3.get("TITLE", "").startswith("Повторное"))
    finally:
        await cleanup(b, phone)
    await b.close()
    print("\nИТОГ:", "все ветки работают" if ok else "ЕСТЬ ОШИБКИ")
    sys.exit(0 if ok else 1)


asyncio.run(main())
