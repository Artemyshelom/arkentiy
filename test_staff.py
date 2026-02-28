"""
Тест: получение активных смен сотрудников через iiko Cloud API.
"""
import asyncio
import json
from pathlib import Path

import httpx

IIKO_CLOUD_BASE = "https://api-ru.iiko.services/api/1"

# Читаем ключ из .env
env_path = Path("/opt/ebidoebi/.env")
api_key = ""
for line in env_path.read_text().splitlines():
    if line.startswith("IIKO_API_KEY="):
        api_key = line.split("=", 1)[1].strip()

# Берём первый org_id
org_ids_path = Path("/opt/ebidoebi/secrets/org_ids.json")
org_ids = json.loads(org_ids_path.read_text())
first_org = list(org_ids.values())[0]
print(f"API key: {api_key[:10]}...")
print(f"Org ID: {first_org}")


async def main():
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Получаем токен
        r = await client.post(
            f"{IIKO_CLOUD_BASE}/access_token",
            json={"apiLogin": api_key},
        )
        r.raise_for_status()
        token = r.json()["token"]
        print(f"Token: {token[:20]}...")
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Пробуем получить активные смены
        print("\n--- POST /employees/active-time-entries ---")
        r2 = await client.post(
            f"{IIKO_CLOUD_BASE}/employees/active-time-entries",
            json={"organizationIds": [first_org]},
            headers=headers,
        )
        print(f"Status: {r2.status_code}")
        print(r2.text[:1000])

        # 3. Пробуем список сотрудников
        print("\n--- POST /employees ---")
        r3 = await client.post(
            f"{IIKO_CLOUD_BASE}/employees",
            json={"organizationIds": [first_org]},
            headers=headers,
        )
        print(f"Status: {r3.status_code}")
        if r3.status_code == 200:
            data = r3.json()
            # Показываем уникальные роли из первых 30 сотрудников
            employees = data.get("employees", [])
            print(f"Всего сотрудников: {len(employees)}")
            roles = set()
            for e in employees[:50]:
                for code in e.get("codes", []):
                    pass
                pos = e.get("position", {})
                if pos:
                    roles.add(pos.get("name", ""))
            print(f"Роли (sample): {sorted(roles)[:20]}")
        else:
            print(r3.text[:500])

        # 4. Пробуем /v2/employees/active-time-entries если есть v2
        print("\n--- POST /v2/employees/active-time-entries ---")
        r4 = await client.post(
            "https://api-ru.iiko.services/api/2/employees/active-time-entries",
            json={"organizationIds": [first_org]},
            headers=headers,
        )
        print(f"Status: {r4.status_code}")
        print(r4.text[:500])


asyncio.run(main())
