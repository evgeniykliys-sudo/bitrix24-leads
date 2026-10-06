"""Клиент REST API Битрикс24 через входящий вебхук.

Вебхук — адрес вида https://ПОРТАЛ.bitrix24.ru/rest/ID/КЛЮЧ/ с правами (scope) на нужные модули.
Лимит Битрикс24 — около 2 запросов в секунду; при превышении приходит QUERY_LIMIT_EXCEEDED, повторяем с паузой.
"""
import asyncio
import logging

import aiohttp


class B24Error(Exception):
    def __init__(self, code: str, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code


class B24:
    def __init__(self, webhook: str, retries: int = 3):
        self.webhook = webhook.rstrip("/") + "/"
        self.retries = retries
        self._session: aiohttp.ClientSession | None = None

    @property
    def portal(self) -> str:
        return self.webhook.split("/rest/")[0]

    async def call(self, method: str, params: dict | None = None):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        for attempt in range(self.retries + 1):
            async with self._session.post(f"{self.webhook}{method}.json", json=params or {}) as r:
                data = await r.json(content_type=None)
            if "error" in data:
                if data["error"] == "QUERY_LIMIT_EXCEEDED" and attempt < self.retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise B24Error(data["error"], data.get("error_description", ""))
            return data["result"]
        raise B24Error("QUERY_LIMIT_EXCEEDED", "лимит запросов не отпустил после повторов")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def lead_url(self, lead_id: int) -> str:
        return f"{self.portal}/crm/lead/details/{lead_id}/"

    def deal_url(self, deal_id: int) -> str:
        return f"{self.portal}/crm/deal/details/{deal_id}/"

    def contact_url(self, contact_id: int) -> str:
        return f"{self.portal}/crm/contact/details/{contact_id}/"


logging.getLogger("aiohttp").setLevel(logging.WARNING)
