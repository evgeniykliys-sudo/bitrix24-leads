"""Telegram-бот «Оставить заявку» → Битрикс24.

Пошаговая форма (FSM): имя → телефон (кнопкой «Поделиться номером» или вручную) → что нужно → подтверждение.
Заявка уходит в CRM без дублей (crm.submit). CRM недоступна — заявка в очереди (outbox), бот дошлёт сам.
"""
import asyncio
import html
import json
import logging
import os
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import crm  # noqa: E402
import outbox  # noqa: E402
from b24 import B24, B24Error  # noqa: E402

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
b24 = B24(os.environ["B24_WEBHOOK"])

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Сбои связи с CRM, при которых заявку надо сохранить и дослать (а не показать клиенту ошибку)
CRM_DOWN = (aiohttp.ClientError, asyncio.TimeoutError, B24Error)


class Form(StatesGroup):
    name = State()
    phone = State()
    need = State()
    confirm = State()


START_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Оставить заявку", callback_data="form")]])
PHONE_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
    resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="или введите номер",
)
CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Отправить", callback_data="send"),
    InlineKeyboardButton(text="✏️ Заново", callback_data="form"),
]])


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Здравствуйте! Оставьте заявку — менеджер свяжется с вами в течение часа.", reply_markup=START_KB)


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заявка отменена.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Если передумаете:", reply_markup=START_KB)


@dp.callback_query(F.data == "form")
async def form_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Form.name)
    await cb.message.answer("Как к вам обращаться?\n\n<i>/cancel — отменить</i>", parse_mode="HTML")
    await cb.answer()


@dp.message(Form.name, F.text)
async def got_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 60:
        await message.answer("Напишите, пожалуйста, имя (от 2 до 60 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Form.phone)
    await message.answer(f"Приятно познакомиться, {html.escape(name)}! Ваш телефон для связи?", reply_markup=PHONE_KB,
                         parse_mode="HTML")


@dp.message(Form.phone, F.contact)
async def got_contact(message: Message, state: FSMContext):
    if message.contact.user_id != message.from_user.id:
        await message.answer("Это чужой контакт. Нажмите «Поделиться номером» или введите свой номер.")
        return
    await take_phone(message, state, message.contact.phone_number)


@dp.message(Form.phone, F.text)
async def got_phone_text(message: Message, state: FSMContext):
    await take_phone(message, state, message.text)


async def take_phone(message: Message, state: FSMContext, raw: str):
    phone = crm.normalize_phone(raw)
    if not phone:
        await message.answer("Не похоже на номер. Например: +7 913 000-11-22", reply_markup=PHONE_KB)
        return
    await state.update_data(phone=phone)
    await state.set_state(Form.need)
    await message.answer("Что вам нужно? Опишите в паре предложений.", reply_markup=ReplyKeyboardRemove())


@dp.message(Form.need, F.text)
async def got_need(message: Message, state: FSMContext):
    need = message.text.strip()
    if len(need) < 3:
        await message.answer("Напишите чуть подробнее, пожалуйста.")
        return
    await state.update_data(need=need[:1000])
    await state.set_state(Form.confirm)
    d = await state.get_data()
    await message.answer(
        f"Проверьте заявку:\n\n👤 {html.escape(d['name'])}\n📱 {d['phone']}\n📝 {html.escape(d['need'])}",
        reply_markup=CONFIRM_KB, parse_mode="HTML")


@dp.message(Form.name)
@dp.message(Form.phone)
@dp.message(Form.need)
async def wrong_type(message: Message):
    await message.answer("Пожалуйста, ответьте текстом. /cancel — отменить заявку.")


@dp.callback_query(Form.confirm, F.data == "send")
async def send(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    payload = {"name": d["name"], "phone": d["phone"], "need": d["need"],
               "tg_username": f"@{cb.from_user.username}" if cb.from_user.username else None}
    try:
        res = await crm.submit(b24, **payload)
    except CRM_DOWN as e:
        logging.exception("CRM недоступна, заявка в очередь")
        item_id = outbox.put(payload, repr(e))
        await cb.message.answer("✅ Заявка принята! Менеджер свяжется с вами в течение часа.")
        await notify_admin(f"⚠️ CRM не ответила — заявка №{item_id} в очереди, бот дошлёт её сам.\n"
                           f"{html.escape(d['name'])}, {d['phone']}: {html.escape(d['need'][:200])}")
        await cb.answer()
        return
    text = {"attached": "✅ Добавили к вашей текущей заявке — менеджер уже в курсе.",
            "repeat": "✅ Рады, что вы снова с нами! Менеджер свяжется в течение часа.",
            "created": "✅ Заявка принята! Менеджер свяжется с вами в течение часа."}[res.action]
    await cb.message.answer(text)
    await notify_admin(admin_text(res, payload))
    await cb.answer()


def admin_text(res: crm.Result, p: dict) -> str:
    head = {"created": "🆕 Новая заявка", "repeat": "🔁 Повторное обращение",
            "attached": "➕ Дополнение к открытой заявке"}[res.action]
    return (f"{head}\n👤 {html.escape(p['name'])} · {p['phone']}" + (f" · {p['tg_username']}" if p["tg_username"] else "")
            + f"\n📝 {html.escape(p['need'][:300])}\n<a href=\"{res.url}\">Открыть в Битрикс24</a>")


async def notify_admin(text: str):
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML", disable_web_page_preview=True)


async def retry_outbox():
    for item in outbox.pending():
        payload = json.loads(item["payload"])
        try:
            res = await crm.submit(b24, **payload)
        except CRM_DOWN as e:
            outbox.failed_again(item["id"], repr(e))
            return  # CRM всё ещё лежит — остальное на следующем круге
        outbox.delivered(item["id"], f"{res.entity} {res.entity_id}")
        await notify_admin(f"📬 Заявка №{item['id']} из очереди доставлена в CRM\n" + admin_text(res, payload))


@dp.message()
async def fallback(message: Message):
    await message.answer("Чтобы оставить заявку — нажмите кнопку:", reply_markup=START_KB)


async def main():
    outbox.init()
    done = await crm.setup(b24)  # источник «Telegram-бот» и поле «Что нужно» — если их ещё нет
    if done:
        logging.info("Настроено в CRM: %s", ", ".join(done))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(retry_outbox, "interval", seconds=60)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        await b24.close()


if __name__ == "__main__":
    asyncio.run(main())
