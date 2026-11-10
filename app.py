"""
Напоминания о приёме лекарств — Telegram-бот с веб-интерфейсом.

Архитектура:
  - Один процесс (aiohttp) обслуживает:
      * POST /webhook/<secret>  — входящие апдейты Telegram (webhook-режим)
      * GET  /app               — веб-интерфейс настройки расписания
      * POST /api/schedule      — чтение/запись расписания (JSON)
      * GET  /healthz           — healthcheck для внешнего пинга
  - Планировщик: asyncio-таск, просыпается каждые 30 секунд, проверяет,
    какие приёмы пора отправить, и шлёт сообщения через Bot API.
  - Хранилище: файл schedule.json рядом с кодом (на Render — на диске сервиса)
    плюс зеркало в GitHub Gist, если задан GIST_TOKEN (переживает рестарты).

Переменные окружения:
  BOT_TOKEN      — токен от BotFather (обязательно)
  WEBHOOK_SECRET — секрет для проверки подлинности webhook (обязательно)
  WEBHOOK_PATH   — путь вебхука (по умолчанию /webhook)
  PORT           — порт (Render задаёт сам)
  TZ             — часовой пояс расписания (по умолчанию Europe/Moscow)
  GIST_TOKEN     — токен GitHub для зеркала расписания (опционально)
  GIST_ID        — id gist для зеркала (опционально)
  GIST_FILENAME  — имя файла в gist (по умолчанию schedule.json)
  ALLOWED_USER_ID— Telegram user id, которому разрешён доступ (опционально)
"""

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp import web, ClientSession, ClientTimeout

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook").rstrip("/") or "/webhook"
PORT = int(os.environ.get("PORT", "8000"))
TZ_NAME = os.environ.get("TZ", "Europe/Moscow")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "").strip()
GIST_ID = os.environ.get("GIST_ID", "").strip()
GIST_FILENAME = os.environ.get("GIST_FILENAME", "schedule.json").strip()
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()
# Fallback chat for reminders. Free Render instances lose their local disk on
# every restart, so the chat id is kept here as well.
DEFAULT_CHAT_ID = os.environ.get("DEFAULT_CHAT_ID", "").strip()
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
GRACE_MINUTES = max(1, int(os.environ.get("GRACE_MINUTES", "20")))

DATA_DIR = os.environ.get("DATA_DIR", "data")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    logging.warning("Unknown TZ %r, falling back to UTC", TZ_NAME)
    TZ = timezone.utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pills")

DEFAULT_SCHEDULE = {
    "timezone": TZ_NAME,
    "items": [],       # [{id, name, time:"HH:MM", doses:int, enabled:bool, days:[0..6]}]
    "updatedAt": None,
}

DEFAULT_STATE = {
    "sent": {},        # {"<id>|<YYYY-MM-DD>": iso_timestamp}
}

# ---------------------------------------------------------------- helpers


def now_local() -> datetime:
    return datetime.now(TZ)


def json_response(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_schedule() -> dict:
    data = read_json(SCHEDULE_FILE, None)
    if not isinstance(data, dict) or "items" not in data:
        data = dict(DEFAULT_SCHEDULE)
        data["items"] = []
    data.setdefault("timezone", TZ_NAME)
    data.setdefault("items", [])
    data.setdefault("updatedAt", None)
    if not data.get("chatId") and DEFAULT_CHAT_ID:
        data["chatId"] = DEFAULT_CHAT_ID
    return data


def save_schedule(data: dict) -> dict:
    data["timezone"] = TZ_NAME
    data["updatedAt"] = now_local().isoformat()
    write_json(SCHEDULE_FILE, data)
    schedule_mirror_to_gist(data)
    return data


def load_state() -> dict:
    data = read_json(STATE_FILE, None)
    if not isinstance(data, dict):
        data = dict(DEFAULT_STATE)
    data.setdefault("sent", {})
    return data


def save_state(data: dict) -> None:
    write_json(STATE_FILE, data)


def normalize_items(raw_items) -> list:
    """Validate and clean incoming items from the web UI."""
    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()[:120]
        if not name:
            continue
        t = str(raw.get("time", "")).strip()
        try:
            hh, mm = t.split(":")
            hh, mm = int(hh), int(mm)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                continue
        except (ValueError, TypeError):
            continue
        try:
            doses = max(1, min(20, int(raw.get("doses", 1))))
        except (ValueError, TypeError):
            doses = 1
        days = raw.get("days")
        if isinstance(days, list):
            days = sorted({int(d) for d in days if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
        else:
            days = [0, 1, 2, 3, 4, 5, 6]
        items.append({
            "id": str(raw.get("id") or f"{name}-{t}-{len(items)}")[:64],
            "name": name,
            "time": f"{hh:02d}:{mm:02d}",
            "doses": doses,
            "enabled": bool(raw.get("enabled", True)),
            "days": days,
        })
    return items


# ---------------------------------------------------------------- Telegram API


class Telegram:
    def __init__(self, session: ClientSession):
        self.session = session
        self.base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    async def call(self, method, payload=None):
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is not set")
        async with self.session.post(
            f"{self.base}/{method}", json=payload or {}, timeout=ClientTimeout(total=30)
        ) as resp:
            body = await resp.json()
            if not body.get("ok"):
                raise RuntimeError(f"{method} failed: {body.get('description')}")
            return body.get("result")

    async def send_message(self, chat_id, text, keyboard=None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        return await self.call("sendMessage", payload)

    async def set_webhook(self, url):
        return await self.call("setWebhook", {"url": url, "secret_token": WEBHOOK_SECRET})

    async def delete_webhook(self):
        return await self.call("deleteWebhook")

    async def get_me(self):
        return await self.call("getMe")


# ---------------------------------------------------------------- gist mirror


def schedule_mirror_to_gist(schedule: dict) -> None:
    """Best-effort mirror of the schedule to a GitHub gist, so settings survive
    a free Render instance restart. Failures are logged and ignored."""
    if not (GIST_TOKEN and GIST_ID):
        return
    content = json.dumps(schedule, ensure_ascii=False, indent=2)

    try:
        import urllib.request

        def req(url, data=None, method="GET"):
            r = urllib.request.Request(url, data=data, method=method)
            r.add_header("Authorization", f"Bearer {GIST_TOKEN}")
            r.add_header("Accept", "application/vnd.github+json")
            r.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.read()

        raw = req(f"https://api.github.com/gists/{GIST_ID}")
        gist = json.loads(raw)
        files = gist.get("files", {})
        current = files.get(GIST_FILENAME, {}).get("content", "")
        if current == content:
            return
        existing = gist.get("files", {}).get(GIST_FILENAME, {}).get("raw_url")
        body = {
            "files": {
                GIST_FILENAME: {
                    "content": content,
                }
            }
        }
        if not existing and GIST_FILENAME not in files:
            body["files"][GIST_FILENAME] = {"content": content}
        req(
            f"https://api.github.com/gists/{GIST_ID}",
            data=json.dumps(body).encode("utf-8"),
            method="PATCH",
        )
        log.info("schedule mirrored to gist %s", GIST_ID)
    except Exception as exc:  # noqa: BLE001
        log.warning("gist mirror failed: %s", exc)


def schedule_restore_from_gist() -> None:
    """If local storage is empty but a gist mirror exists, restore from it."""
    if not (GIST_TOKEN and GIST_ID):
        return
    try:
        import urllib.request

        r = urllib.request.Request(f"https://api.github.com/gists/{GIST_ID}")
        r.add_header("Authorization", f"Bearer {GIST_TOKEN}")
        r.add_header("Accept", "application/vnd.github+json")
        with urllib.request.urlopen(r, timeout=15) as resp:
            gist = json.loads(resp.read())
        fileinfo = gist.get("files", {}).get(GIST_FILENAME)
        if not fileinfo:
            return
        data = json.loads(fileinfo.get("content", "{}"))
        if isinstance(data, dict) and data.get("items"):
            write_json(SCHEDULE_FILE, data)
            log.info("schedule restored from gist: %d items", len(data["items"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("gist restore failed: %s", exc)


# ---------------------------------------------------------------- scheduling


def due_items(schedule: dict, state: dict, now: datetime):
    """Return items that must be sent right now (once per day)."""
    tz = TZ
    today = now.date()
    due = []
    for item in schedule.get("items", []):
        if not item.get("enabled", True):
            continue
        days = item.get("days")
        if isinstance(days, list) and days and today.weekday() not in days:
            continue
        try:
            hh, mm = [int(x) for x in item["time"].split(":")]
        except (KeyError, ValueError):
            continue
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # Fire within a grace window after the target minute. The window is wide
        # enough to still deliver a dose missed while the free instance slept.
        if not (target <= now < target + timedelta(minutes=GRACE_MINUTES)):
            continue
        key = f"{item.get('id')}|{today.isoformat()}"
        if state.get("sent", {}).get(key):
            continue
        due.append(item)
    return due


def format_dose(item) -> str:
    name = html.escape(str(item.get("name", "")))
    doses = int(item.get("doses", 1))
    return f"{name} — {doses} шт."


async def scheduler_loop(tg: Telegram, session: ClientSession):
    log.info("scheduler started (tz=%s)", TZ_NAME)
    while True:
        try:
            schedule = load_schedule()
            state = load_state()
            now = now_local()
            for item in due_items(schedule, state, now):
                chat_id = schedule.get("chatId")
                if not chat_id:
                    log.warning("no chatId in schedule, skipping")
                    continue
                text = (
                    f"⏰ <b>Пора принять лекарство</b>\n\n"
                    f"{format_dose(item)}\n"
                    f"Время: {item['time']}"
                )
                try:
                    await tg.send_message(chat_id, text)
                    state.setdefault("sent", {})[f"{item['id']}|{now.date().isoformat()}"] = now.isoformat()
                    save_state(state)
                    log.info("sent reminder: %s", format_dose(item))
                except Exception as exc:  # noqa: BLE001
                    log.error("send failed for %s: %s", item.get("id"), exc)
        except Exception as exc:  # noqa: BLE001
            log.error("scheduler iteration failed: %s", exc)
        await asyncio.sleep(30)


# ---------------------------------------------------------------- web UI

INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Напоминания о лекарствах</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --fg:#14161a; --muted:#6b7280;
          --line:#e5e7eb; --accent:#2563eb; --danger:#dc2626; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1115; --card:#181b21; --fg:#e8eaed; --muted:#9aa3af; --line:#2a2f38; --accent:#5b8cff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--fg);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:720px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); margin-bottom:20px; font-size:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; margin-bottom:16px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .grow { flex:1 1 160px; }
  input[type=text], input[type=number], input[type=time], select {
    width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:10px;
    background:var(--bg); color:var(--fg); font-size:15px;
  }
  label { display:block; font-size:13px; color:var(--muted); margin:0 0 6px; }
  .field { margin-bottom:14px; }
  button { padding:10px 16px; border-radius:10px; border:1px solid var(--line); background:var(--card);
           color:var(--fg); font-size:14px; cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  button.danger { color:var(--danger); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .item { display:flex; align-items:center; gap:12px; padding:12px 0; border-top:1px solid var(--line); }
  .item:first-child { border-top:none; }
  .item .meta { flex:1; }
  .item .nm { font-weight:600; }
  .item .tm { color:var(--muted); font-size:13px; }
  .badge { font-size:12px; padding:2px 8px; border-radius:999px; background:var(--bg);
           border:1px solid var(--line); color:var(--muted); }
  .empty { color:var(--muted); text-align:center; padding:22px 0; }
  .status { padding:10px 12px; border-radius:10px; margin-bottom:14px; font-size:14px; }
  .status.ok { background:rgba(37,99,235,.1); border:1px solid var(--accent); }
  .status.err { background:rgba(220,38,38,.1); border:1px solid var(--danger); }
  .hint { color:var(--muted); font-size:13px; margin-top:6px; }
  .days { display:flex; gap:6px; flex-wrap:wrap; }
  .days button { padding:6px 11px; font-size:13px; }
  .days button.on { background:var(--accent); border-color:var(--accent); color:#fff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Напоминания о лекарствах</h1>
  <div class="sub">Расписание приёмов. Бот пришлёт сообщение в Telegram в указанное время.</div>
  <div id="status" class="status" style="display:none"></div>

  <div class="card">
    <div class="row" style="margin-bottom:14px">
      <button class="primary" id="add">Добавить приём</button>
      <button id="saveTop">Сохранить</button>
      <span class="grow"></span>
      <span class="badge" id="tzbadge"></span>
    </div>
    <div id="list"><div class="empty">Загрузка…</div></div>
  </div>

  <div class="card" id="form" style="display:none">
    <div class="field"><label>Название лекарства</label>
      <input type="text" id="f_name" placeholder="Например, Амоксициллин"></div>
    <div class="row">
      <div class="field grow"><label>Время</label><input type="time" id="f_time" value="09:00"></div>
      <div class="field" style="width:120px"><label>Количество, шт.</label>
        <input type="number" id="f_doses" min="1" max="20" value="1"></div>
    </div>
    <div class="field"><label>Дни недели</label><div class="days" id="f_days"></div></div>
    <div class="row">
      <button class="primary" id="f_save">Добавить</button>
      <button id="f_cancel">Отмена</button>
    </div>
  </div>

  <div class="card">
    <div class="field"><label>Часовой пояс</label>
      <input type="text" id="tz" readonly>
      <div class="hint">Расписание считается в часовом поясе сервера: <span id="tzname"></span></div>
    </div>
    <div class="field"><label>Код доступа</label>
      <input type="text" id="code" placeholder="Введите код доступа">
      <div class="hint">Сохраняется в этом браузере. Если код не задан, поле можно оставить пустым.</div>
    </div>
    <div class="field"><label>Тестовое сообщение</label>
      <button id="test">Прислать тест сейчас</button>
      <div class="hint">Проверяет, что бот работает и сообщения доходят.</div>
    </div>
  </div>
</div>

<script>
const DAY_NAMES = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
let schedule = { items: [], timezone: "", chatId: null };
let editingId = null;

const $ = (id) => document.getElementById(id);

function showStatus(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = "status " + kind;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 4000);
}

async function api(path, opts) {
  const headers = Object.assign({ "Content-Type": "application/json" }, (opts && opts.headers) || {});
  const code = ($("code") ? $("code").value.trim() : "") || localStorage.getItem("access_code") || "";
  if (code) headers["X-Access-Code"] = code;
  const res = await fetch(path, Object.assign({ headers }, opts));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function renderDays(selected) {
  const box = $("f_days");
  box.innerHTML = "";
  for (let i = 0; i < 7; i++) {
    const b = document.createElement("button");
    b.textContent = DAY_NAMES[i];
    if (selected.includes(i)) b.classList.add("on");
    b.onclick = () => {
      const on = b.classList.contains("on");
      if (on) b.classList.remove("on"); else b.classList.add("on");
    };
    box.appendChild(b);
  }
}

function currentDays() {
  return [...$("f_days").querySelectorAll("button.on")].map((b, i) => DAY_NAMES.indexOf(b.textContent));
}

function render() {
  const list = $("list");
  if (!schedule.items.length) {
    list.innerHTML = '<div class="empty">Пока ничего не запланировано.<br>Нажмите «Добавить приём».</div>';
    return;
  }
  list.innerHTML = "";
  for (const it of schedule.items) {
    const div = document.createElement("div");
    div.className = "item";
    const days = (it.days && it.days.length === 7) ? "каждый день"
      : (it.days || []).map(d => DAY_NAMES[d]).join(", ");
    div.innerHTML =
      '<div class="meta"><div class="nm"></div><div class="tm"></div></div>' +
      '<span class="badge"></span>' +
      '<button class="edit">Изм.</button><button class="danger del">Удалить</button>';
    div.querySelector(".nm").textContent = it.name;
    div.querySelector(".tm").textContent = it.time + " · " + it.doses + " шт. · " + days;
    div.querySelector(".badge").textContent = it.enabled ? "вкл" : "выкл";
    div.querySelector(".edit").onclick = () => startEdit(it);
    div.querySelector(".del").onclick = async () => {
      if (!confirm("Удалить «" + it.name + "»?")) return;
      schedule.items = schedule.items.filter(x => x.id !== it.id);
      render();
      await persist();
    };
    list.appendChild(div);
  }
}

function startEdit(it) {
  editingId = it.id;
  $("f_name").value = it.name;
  $("f_time").value = it.time;
  $("f_doses").value = it.doses;
  renderDays(it.days || [0,1,2,3,4,5,6]);
  $("form").style.display = "block";
  $("f_save").textContent = "Сохранить";
  $("form").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function persist() {
  try {
    const data = await api("/api/schedule", { method: "PUT", body: JSON.stringify({ items: schedule.items }) });
    schedule.items = data.items;
    showStatus("Сохранено", "ok");
    render();
  } catch (e) {
    showStatus("Ошибка сохранения: " + e.message, "err");
  }
}

async function load() {
  try {
    schedule = await api("/api/schedule");
    $("tz").value = schedule.timezone || "";
    $("tzname").textContent = schedule.timezone || "";
    $("tzbadge").textContent = schedule.timezone || "";
    render();
  } catch (e) {
    $("list").innerHTML = '<div class="empty">Не удалось загрузить расписание.</div>';
  }
}

$("add").onclick = () => {
  editingId = null;
  $("f_name").value = ""; $("f_time").value = "09:00"; $("f_doses").value = "1";
  renderDays([0,1,2,3,4,5,6]);
  $("f_save").textContent = "Добавить";
  $("form").style.display = "block";
  $("f_name").focus();
};
$("code").value = localStorage.getItem("access_code") || "";
$("code").oninput = () => { localStorage.setItem("access_code", $("code").value.trim()); };
$("code").onchange = () => { load(); };
$("f_cancel").onclick = () => { $("form").style.display = "none"; editingId = null; };
$("f_save").onclick = async () => {
  const name = $("f_name").value.trim();
  if (!name) { showStatus("Укажите название лекарства", "err"); return; }
  const item = {
    id: editingId || (name + "-" + Date.now()),
    name,
    time: $("f_time").value || "09:00",
    doses: parseInt($("f_doses").value, 10) || 1,
    enabled: true,
    days: currentDays().length ? currentDays() : [0,1,2,3,4,5,6],
  };
  if (editingId) schedule.items = schedule.items.map(x => (x.id === editingId ? item : x));
  else schedule.items.push(item);
  $("form").style.display = "none";
  editingId = null;
  render();
  await persist();
};
$("saveTop").onclick = persist;
$("test").onclick = async () => {
  try {
    await api("/api/test", { method: "POST" });
    showStatus("Тестовое сообщение отправлено", "ok");
  } catch (e) {
    showStatus("Ошибка: " + e.message, "err");
  }
};

load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP handlers


def make_handlers(tg: Telegram):
    def authorized(request) -> bool:
        """Access-code check for the web UI and its API."""
        if not ACCESS_CODE:
            return True
        supplied = request.headers.get("X-Access-Code", "")
        if not supplied:
            token = request.query.get("code", "")
            supplied = token
        return supplied == ACCESS_CODE

    async def healthz(request):
        return web.Response(text="ok")

    async def index(request):
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def api_schedule_get(request):
        if not authorized(request):
            return json_response({"error": "forbidden"}, status=403)
        return json_response(load_schedule())

    async def api_schedule_put(request):
        if not authorized(request):
            return json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return json_response({"error": "invalid json"}, status=400)
        items = normalize_items(body.get("items"))
        schedule = load_schedule()
        schedule["items"] = items
        save_schedule(schedule)
        log.info("schedule updated: %d items", len(items))
        return json_response(schedule)

    async def api_test(request):
        if not authorized(request):
            return json_response({"error": "forbidden"}, status=403)
        schedule = load_schedule()
        chat_id = schedule.get("chatId")
        if not chat_id:
            return json_response({"error": "no chat id yet"}, status=400)
        try:
            await tg.send_message(chat_id, "✅ Тест: бот работает. Это тестовое сообщение.")
        except Exception as exc:  # noqa: BLE001
            return json_response({"error": str(exc)}, status=502)
        return json_response({"ok": True})

    async def webhook(request):
        if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403)
        try:
            update = await request.json()
        except (ValueError, TypeError):
            return web.Response(status=400)
        asyncio.create_task(handle_update(tg, update))
        return web.Response(text="ok")

    return [
        web.get("/healthz", healthz),
        web.get("/", index),
        web.get("/app", index),
        web.get("/api/schedule", api_schedule_get),
        web.put("/api/schedule", api_schedule_put),
        web.post("/api/test", api_test),
        web.post(WEBHOOK_PATH, webhook),
        web.post(WEBHOOK_PATH + "/{secret}", webhook),
    ]


async def handle_update(tg: Telegram, update: dict):
    """Very small command handler: /start, /schedule, /stop."""
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id:
            return

        if ALLOWED_USER_ID and str(chat_id) != ALLOWED_USER_ID:
            return

        schedule = load_schedule()
        schedule["chatId"] = chat_id
        write_json(SCHEDULE_FILE, schedule)

        if text.startswith("/start") or text.startswith("/help"):
            items = schedule.get("items", [])
            lines = ["👋 Бот напоминаний о лекарствах.", ""]
            if items:
                lines.append("Ваше расписание:")
                for it in items:
                    lines.append(f"• {it['time']} — {it['name']} ({it['doses']} шт.)")
            else:
                lines.append("Расписание пока пустое. Настройте его в веб-интерфейсе.")
            await tg.send_message(chat_id, "\n".join(lines))
        elif text.startswith("/schedule"):
            items = schedule.get("items", [])
            lines = ["Расписание приёмов:" if items else "Расписание пустое."]
            for it in items:
                lines.append(f"• {it['time']} — {it['name']} ({it['doses']} шт.)")
            await tg.send_message(chat_id, "\n".join(lines))
        elif text.startswith("/stop"):
            await tg.send_message(chat_id, "Ок. Напоминания остановлены.")
            schedule["items"] = []
            save_schedule(schedule)
    except Exception as exc:  # noqa: BLE001
        log.error("update handling failed: %s", exc)


# ---------------------------------------------------------------- bootstrap


async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not set — exiting")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    schedule_restore_from_gist()

    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as session:
        tg = Telegram(session)

        try:
            me = await tg.get_me()
            log.info("bot: @%s (%s)", me.get("username"), me.get("first_name"))
        except Exception as exc:  # noqa: BLE001
            # A blocked/unreachable Bot API must not stop the web UI from serving.
            log.warning("could not reach Bot API at startup: %s", exc)

        app = web.Application()
        app.add_routes(make_handlers(tg))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info("listening on :%s", PORT)

        # Register the public webhook if the platform told us our URL.
        public_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
        if public_url:
            hook = f"{public_url.rstrip('/')}{WEBHOOK_PATH}"
            try:
                await tg.set_webhook(hook)
                log.info("webhook registered: %s", hook)
            except Exception as exc:  # noqa: BLE001
                log.warning("webhook registration failed: %s", exc)
        else:
            log.warning("PUBLIC_URL / RENDER_EXTERNAL_URL not set — webhook not registered")

        sched = asyncio.create_task(scheduler_loop(tg, session))
        try:
            await asyncio.Event().wait()
        finally:
            sched.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
