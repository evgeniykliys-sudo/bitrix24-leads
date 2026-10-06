"""Живая проверка правила против дублей на настоящем портале Битрикс24, в его текущем режиме CRM.
Создаёт записи с пометкой [тест] и в конце удаляет их.

    python tests/live_dedupe.py
"""
import asyncio
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")
import crm  # noqa: E402
from b24 import B24  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


async def contacts_by_phone(b, phone):
    return await b.call("crm.contact.list", {"filter": {"PHONE": phone}, "select": ["ID"]})


async def main():
    b = B24(os.environ["B24_WEBHOOK"])
    mode = await crm.crm_mode(b)
    print("Режим CRM:", "классический (лиды)" if mode == crm.CLASSIC else "простой (контакт + сделка)")
    ent = "lead" if mode == crm.CLASSIC else "deal"
    digits = f"900{random.randint(1000000, 9999999)}"
    phone = "+7" + digits
    other_format = f"8 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
    created = set()

    r1 = await crm.submit(b, name="[тест] Анна", phone=phone, need="Бот для записи клиентов", tg_username="@anna_test")
    created.add((r1.entity, r1.entity_id))
    check(f"1. новый телефон → новая заявка ({r1.entity} {r1.entity_id})", r1.action == "created")

    check(f"   «{other_format}» нормализуется в тот же номер", crm.normalize_phone(other_format) == phone)
    r2 = await crm.submit(b, name="[тест] Анна", phone=crm.normalize_phone(other_format), need="Ещё хочу рассылку")
    check("2. повторная заявка, прошлая открыта → комментарий в ту же заявку, без дубля",
          r2.action == "attached" and r2.entity_id == r1.entity_id)
    comments = await b.call("crm.timeline.comment.list", {"filter": {"ENTITY_ID": r1.entity_id, "ENTITY_TYPE": ent}})
    check("   повторная заявка видна в ленте", any("рассылку" in c["COMMENT"] for c in comments))
    if mode == crm.SIMPLE:
        contacts = await contacts_by_phone(b, phone)
        deals = await b.call("crm.deal.list", {"filter": {"CONTACT_ID": contacts[0]["ID"]}, "select": ["ID"]})
        check(f"   один контакт и одна сделка (контактов: {len(contacts)}, сделок: {len(deals)})",
              len(contacts) == 1 and len(deals) == 1)

    # закрываем заявку как проигранную → следующее обращение = повторное
    if mode == crm.SIMPLE:
        await b.call("crm.deal.update", {"id": r1.entity_id, "fields": {"STAGE_ID": "LOSE"}})
    else:
        await b.call("crm.lead.update", {"id": r1.entity_id, "fields": {"STATUS_ID": "JUNK"}})
        cid = await b.call("crm.contact.add", {"fields": {"NAME": "[тест] Анна", "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}]}})
        created.add(("contact", int(cid)))
    r3 = await crm.submit(b, name="[тест] Анна", phone=phone, need="Вернулась через месяц")
    created.add((r3.entity, r3.entity_id))
    item = await b.call(f"crm.{ent}.get", {"id": r3.entity_id})
    contact_id = int((await contacts_by_phone(b, phone))[0]["ID"])
    check(f"3. прошлая закрыта, клиент известен → «Повторное обращение» к тому же контакту ({ent} {r3.entity_id})",
          r3.action == "repeat" and item["TITLE"].startswith("Повторное") and int(item["CONTACT_ID"]) == contact_id)
    check(f"   источник «{crm.SOURCE_NAME}» и поле «Что нужно» заполнены",
          item["SOURCE_ID"] == crm.SOURCE_ID and item.get(crm.NEED_FIELD) == "Вернулась через месяц")

    # уборка: всё созданное этим тестом
    for c in await contacts_by_phone(b, phone):
        created.add(("contact", int(c["ID"])))
    for e, i in created:
        try:
            await b.call(f"crm.{e}.delete", {"id": i})
        except Exception:
            pass
    await b.close()
    print("\nИТОГ:", "все ветки работают" if ok else "ЕСТЬ ОШИБКИ")
    sys.exit(0 if ok else 1)


asyncio.run(main())
