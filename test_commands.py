"""Unit-test the Telegram command parsing (no network needed)."""
import importlib.util
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("TZ", "Europe/Moscow")
os.environ.setdefault("DATA_DIR", "data_test")

spec = importlib.util.spec_from_file_location("app", r"H:\AI\Minimax\pill-reminder-bot\app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# 1. plain add
item, err = app.parse_add("/add 09:30 Амоксициллин")
print("1. add time+name       :", item, err)
assert err is None and item["time"] == "09:30" and item["name"] == "Амоксициллин"
assert item["doses"] == 1 and item["days"] == [0, 1, 2, 3, 4, 5, 6]
assert item["enabled"] is True

# 2. add with dose count
item, err = app.parse_add("/add 21:00 Амоксициллин 2")
print("2. add with doses      :", item, err)
assert item["doses"] == 2 and item["name"] == "Амоксициллин"

# 3. add with weekdays
item, err = app.parse_add("/add 08:00 Аспирин 1 пн ср пт")
print("3. add with days       :", item, err)
assert item["days"] == [0, 2, 4], item["days"]

# 4. add with weekdays and dose count
item, err = app.parse_add("/add 08:00 Аспирин 3 пн,ср,пт")
print("4. days + doses        :", item, err)
assert item["doses"] == 3 and item["days"] == [0, 2, 4]

# 5. explicit every day
item, err = app.parse_add("/add 07:00 Капли ежедневно")
print("5. every day           :", item, err)
assert item["days"] == [0, 1, 2, 3, 4, 5, 6]

# 6. multi-word name
item, err = app.parse_add("/add 10:15 Витамин D 3")
print("6. multi word name     :", item, err)
assert item["name"] == "Витамин D" and item["doses"] == 3

# 7. missing arguments
for bad in ("/add", "/add 09:30", "/add 25:00 Что-то", "/add abc Что-то"):
    item, err = app.parse_add(bad)
    print("7. rejected", repr(bad), "->", err)
    assert item is None and err

# 8. labels
print("8. days label          :", app.days_label([0, 2, 4]), "|", app.days_label([0, 1, 2, 3, 4, 5, 6]))
assert app.days_label([0, 2, 4]) == "пн, ср, пт"
assert app.days_label([0, 1, 2, 3, 4, 5, 6]) == "каждый день"

# 9. list rendering
lines = app.format_items([
    {"time": "09:30", "name": "Амоксициллин", "doses": 2, "enabled": True, "days": [0, 1, 2, 3, 4, 5, 6]},
    {"time": "21:00", "name": "Витамин", "doses": 1, "enabled": False, "days": [0, 2, 4]},
])
print("9. list lines          :", lines)
assert lines[0].startswith("1. 09:30 — Амоксициллин (2 шт.)")
assert "выключено" in lines[1]

print("\nALL COMMAND TESTS PASSED")
