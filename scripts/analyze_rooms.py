"""
Анализирует все комнаты Leo и выявляет группы (3+ участников).
Запуск: sudo -u ai bash -c 'set -a; source /opt/ai/bridge/.env; set +a; python3 /tmp/analyze_rooms.py'
"""
import os
import json
import urllib.request
import urllib.parse
import time

TOKEN = os.environ.get("BRIDGE_ACCESS_TOKEN")
BASE = os.environ.get("MATRIX_HOMESERVER_URL", "https://mtx.respectrb.ru") + "/_matrix/client/v3"

if not TOKEN:
    print("❌ BRIDGE_ACCESS_TOKEN не настроен")
    exit(1)


def matrix_get(path, timeout=5):
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# 1. Получаем список всех комнат
print("📋 Получаем список комнат...")
data = matrix_get("/joined_rooms")
rooms = data.get("joined_rooms", [])
print(f"   Всего: {len(rooms)} комнат\n")

# 2. Для каждой — узнаём количество участников и имя
groups = []  # 3+ участников
dms = []     # ≤2
errors = []

print(f"🔍 Анализируем по одной...")
for i, room_id in enumerate(rooms, 1):
    enc = urllib.parse.quote(room_id)
    
    # joined_members
    try:
        members_data = matrix_get(f"/rooms/{enc}/joined_members")
        count = len(members_data.get("joined", {}))
    except Exception as e:
        errors.append((room_id, f"members: {e}"))
        continue
    
    # имя
    try:
        name_data = matrix_get(f"/rooms/{enc}/state/m.room.name")
        name = name_data.get("name", "<no name>")
    except:
        name = "<no name>"
    
    if count > 2:
        groups.append((room_id, count, name))
    else:
        dms.append((room_id, count, name))
    
    # Прогресс каждые 10
    if i % 10 == 0:
        print(f"   ...{i}/{len(rooms)}")
    
    # Не флудим API
    time.sleep(0.05)

# 3. Отчёт
print()
print("=" * 60)
print(f"ИТОГО:")
print(f"  DM (≤2 чел):     {len(dms)}")
print(f"  ГРУППЫ (3+ чел): {len(groups)}")
print(f"  Ошибок:          {len(errors)}")
print("=" * 60)

if groups:
    print()
    print("🚨 ГРУППЫ ИЗ КОТОРЫХ НУЖНО ВЫЙТИ:")
    for room_id, count, name in sorted(groups, key=lambda x: -x[1]):
        print(f"  [{count:3d} чел] «{name[:50]}»")
        print(f"             {room_id}")

if errors:
    print()
    print(f"⚠️  Ошибки доступа ({len(errors)}):")
    for r, e in errors[:5]:
        print(f"  {r}: {e[:80]}")

print()
print("✓ Анализ завершён")
