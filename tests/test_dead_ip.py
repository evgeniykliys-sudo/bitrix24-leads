"""Проверка устойчивости к недоступному IP Битрикс24 (найден вживую: из домашней сети 109.120.182.255 не отвечает,
остальные 12 адресов — да). Подставной DNS отдаёт «мёртвый» адрес ПЕРВЫМ — клиент должен сам перейти к рабочему.

    python tests/test_dead_ip.py
"""
import asyncio
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from aiohttp.abc import AbstractResolver  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")
from b24 import B24  # noqa: E402

DEAD, GOOD = "109.120.182.255", "95.163.249.170"


class DeadFirstResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}
                for ip in (DEAD, GOOD)]

    async def close(self):
        pass


async def main():
    b = B24(os.environ["B24_WEBHOOK"], resolver=DeadFirstResolver())
    t = time.monotonic()
    mode = await b.call("crm.settings.mode.get")
    took = time.monotonic() - t
    await b.close()
    ok = mode in (1, 2) and took < 10
    print(("✅" if ok else "❌"), f"«мёртвый» адрес первым → ответ получен через {took:.1f} с (перешёл на рабочий адрес)")
    sys.exit(0 if ok else 1)


asyncio.run(main())
