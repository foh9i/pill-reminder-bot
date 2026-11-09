"""Unit-test the due-item logic of the scheduler (no network needed)."""
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("TZ", "Europe/Moscow")
os.environ.setdefault("DATA_DIR", "data_test")

spec = importlib.util.spec_from_file_location("app", r"H:\AI\Minimax\pill-reminder-bot\app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

TZ = ZoneInfo("Europe/Moscow")
now = datetime(2026, 11, 9, 9, 30, tzinfo=TZ)   # Monday

schedule = {
    "items": [
        {"id": "a", "name": "Амоксициллин", "time": "09:30", "doses": 2, "enabled": True, "days": [0,1,2,3,4,5,6]},
        {"id": "b", "name": "Витамин D", "time": "21:30", "doses": 1, "enabled": True, "days": [0,2,4]},
        {"id": "c", "name": "Отключённое", "time": "09:30", "doses": 1, "enabled": False, "days": [0]},
        {"id": "d", "name": "Только по средам", "time": "09:30", "doses": 1, "enabled": True, "days": [2]},
    ]
}

print("TZ used by app:", app.TZ)

# 1. exact time -> due
state = {"sent": {}}
due = app.due_items(schedule, state, now)
print("1. due at 09:30      :", [d["id"] for d in due])
assert [d["id"] for d in due] == ["a"], due

# 2. already sent today -> not due again
state = {"sent": {"a|2026-11-09": "2026-11-09T09:30:00+03:00"}}
due = app.due_items(schedule, state, now)
print("2. already sent       :", [d["id"] for d in due])
assert due == [], due

# 3. next day -> due again (new date key)
tomorrow = now + timedelta(days=1)
state = {"sent": {"a|2026-11-09": "x"}}
due = app.due_items(schedule, state, tomorrow)
print("3. next day           :", [d["id"] for d in due])
assert [d["id"] for d in due] == ["a"], due

# 4. grace window: 10 min later still due, 30 min later not
state = {"sent": {}}
due = app.due_items(schedule, state, now + timedelta(minutes=10))
print("4. +10 min (grace)    :", [d["id"] for d in due])
assert [d["id"] for d in due] == ["a"], due

state = {"sent": {}}
due = app.due_items(schedule, state, now + timedelta(minutes=30))
print("5. +30 min (expired)  :", [d["id"] for d in due])
assert due == [], due

# 6. before the target time -> not due
state = {"sent": {}}
due = app.due_items(schedule, state, now - timedelta(minutes=1))
print("6. one minute before  :", [d["id"] for d in due])
assert due == [], due

# 7. disabled item never fires; weekday filter respected (Mon is not in [2])
state = {"sent": {}}
due = app.due_items(schedule, state, now)
ids = [d["id"] for d in due]
print("7. disabled/wrongday  :", ids)
assert "c" not in ids and "d" not in ids, ids

# 8. weekday filter: days:[2] means Wednesday (Python weekday(): 0=Mon)
wednesday = datetime(2026, 11, 11, 9, 30, tzinfo=TZ)
state = {"sent": {}}
due = app.due_items(schedule, state, wednesday)
print("8. Wednesday items    :", [d["id"] for d in due])
assert [d["id"] for d in due] == ["a", "d"], due

# 8b. on Tuesday the Wednesday-only item must not fire
tuesday2 = datetime(2026, 11, 10, 9, 30, tzinfo=TZ)
state = {"sent": {}}
due = app.due_items(schedule, state, tuesday2)
print("8b. Tuesday items     :", [d["id"] for d in due])
assert [d["id"] for d in due] == ["a"], due

# 9. formatting
print("9. format             :", app.format_dose(schedule["items"][0]))
assert app.format_dose(schedule["items"][0]) == "Амоксициллин — 2 шт."

# 10. html escaping in dose text
print("10. escaped           :", app.format_dose({"name": "<b>A&B</b>", "doses": 1}))
assert "<b>" not in app.format_dose({"name": "<b>A&B</b>", "doses": 1})

print("\nALL SCHEDULER TESTS PASSED")
