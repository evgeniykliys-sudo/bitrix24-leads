"""Заявки → Битрикс24: нормализация телефона, настройка CRM через API, заявка без дублей.

Битрикс24 работает в одном из двух режимов, и интеграция обязана их различать (crm.settings.mode.get):
- КЛАССИЧЕСКИЙ (1) — заявки живут как лиды;
- ПРОСТОЙ (2) — лидов нет: заявка = контакт + сделка (новые порталы по умолчанию в нём;
  созданный через API лид Битрикс сам сразу превращает в контакт и сделку).

Правило против дублей (по телефону), в терминах режима:
- есть ОТКРЫТАЯ заявка (лид в работе / сделка не закрыта) → новую не создаём, пишем комментарием в её ленту;
- открытой нет, но клиент уже есть в контактах → новая заявка «Повторное обращение», привязанная к контакту;
- ничего нет → новая заявка.
"""
import re
from dataclasses import dataclass

from b24 import B24

SOURCE_ID = "TELEGRAM_BOT"
SOURCE_NAME = "Telegram-бот"
NEED_FIELD = "UF_CRM_NEED"  # Битрикс сам добавляет префикс UF_CRM_ к имени NEED


def normalize_phone(raw: str) -> str | None:
    """«8 (913) 000-11-22», «+7 913 0001122», «9130001122» → «+79130001122». Нераспознанное → None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if raw.strip().startswith("+") and 11 <= len(digits) <= 15 and not digits.startswith("7"):
        return "+" + digits  # иностранный номер
    return None


CLASSIC, SIMPLE = 1, 2


async def crm_mode(b24: B24) -> int:
    return int(await b24.call("crm.settings.mode.get"))


async def setup(b24: B24) -> list[str]:
    """Идемпотентная настройка CRM под интеграцию: повторный запуск ничего не дублирует. Возвращает, что сделано."""
    done = []
    sources = await b24.call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}})
    if not any(s["STATUS_ID"] == SOURCE_ID for s in sources):
        await b24.call("crm.status.add", {"fields": {"ENTITY_ID": "SOURCE", "STATUS_ID": SOURCE_ID,
                                                     "NAME": SOURCE_NAME, "SORT": 5}})
        done.append(f"источник лидов «{SOURCE_NAME}»")
    for entity, label in (("lead", "лида"), ("deal", "сделки")):  # поле в обоих — режим портала могут переключить
        fields = await b24.call(f"crm.{entity}.userfield.list", {"filter": {"FIELD_NAME": NEED_FIELD}})
        if not fields:
            await b24.call(f"crm.{entity}.userfield.add", {"fields": {
                "FIELD_NAME": "NEED", "USER_TYPE_ID": "string", "XML_ID": "NEED",
                "EDIT_FORM_LABEL": {"ru": "Что нужно клиенту"}, "LIST_COLUMN_LABEL": {"ru": "Что нужно"},
                "SETTINGS": {"ROWS": 3},
            }})
            done.append(f"поле {label} «Что нужно клиенту»")
    return done


@dataclass
class Result:
    action: str  # created | repeat | attached
    entity: str  # lead | deal
    entity_id: int
    url: str


async def find_by_phone(b24: B24, phone: str) -> dict:
    res = await b24.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": [phone]})
    return res if isinstance(res, dict) else {}  # Битрикс отдаёт [] вместо {}, если ничего не нашлось


async def _comment(b24: B24, entity: str, entity_id: int, source_id: str, need: str) -> None:
    src = SOURCE_NAME if source_id == SOURCE_ID else source_id
    await b24.call("crm.timeline.comment.add", {"fields": {
        "ENTITY_ID": entity_id, "ENTITY_TYPE": entity, "COMMENT": f"Повторная заявка ({src}):\n{need}"}})


async def submit(b24: B24, *, name: str, phone: str, need: str, tg_username: str | None = None,
                 source_id: str = SOURCE_ID, mode: int | None = None) -> Result:
    mode = mode or await crm_mode(b24)
    found = await find_by_phone(b24, phone)
    contact_ids = sorted(int(x) for x in found.get("CONTACT", []))
    title = ("Повторное обращение: " if contact_ids else "Заявка: ") + need[:60]
    im = [{"VALUE": tg_username, "VALUE_TYPE": "TELEGRAM"}] if tg_username else None

    if mode == CLASSIC:
        for lead_id in sorted((int(x) for x in found.get("LEAD", [])), reverse=True):
            lead = await b24.call("crm.lead.get", {"id": lead_id})
            if lead.get("STATUS_SEMANTIC_ID") == "P":  # P — в работе; S/F — закрыт успешно/неуспешно
                await _comment(b24, "lead", lead_id, source_id, need)
                return Result("attached", "lead", lead_id, b24.lead_url(lead_id))
        fields = {"TITLE": title, "NAME": name, "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}],
                  "SOURCE_ID": source_id, "COMMENTS": need, NEED_FIELD: need}
        if im:
            fields["IM"] = im
        if contact_ids:
            fields["CONTACT_ID"] = contact_ids[-1]
        lead_id = int(await b24.call("crm.lead.add", {"fields": fields}))
        return Result("repeat" if contact_ids else "created", "lead", lead_id, b24.lead_url(lead_id))

    # ПРОСТОЙ режим: контакт + сделка
    for contact_id in reversed(contact_ids):
        deals = await b24.call("crm.deal.list", {"filter": {"CONTACT_ID": contact_id, "STAGE_SEMANTIC_ID": "P"},
                                                 "order": {"ID": "DESC"}, "select": ["ID"]})
        if deals:
            deal_id = int(deals[0]["ID"])
            await _comment(b24, "deal", deal_id, source_id, need)
            return Result("attached", "deal", deal_id, b24.deal_url(deal_id))
    if contact_ids:
        contact_id = contact_ids[-1]
    else:
        cf = {"NAME": name, "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}], "SOURCE_ID": source_id}
        if im:
            cf["IM"] = im
        contact_id = int(await b24.call("crm.contact.add", {"fields": cf}))
    deal_id = int(await b24.call("crm.deal.add", {"fields": {
        "TITLE": title, "CONTACT_ID": contact_id, "SOURCE_ID": source_id, "COMMENTS": need, NEED_FIELD: need}}))
    return Result("repeat" if contact_ids else "created", "deal", deal_id, b24.deal_url(deal_id))
