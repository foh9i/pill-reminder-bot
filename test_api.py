"""Local API test for the pill reminder bot."""
import json, urllib.request

BASE = "http://127.0.0.1:8099"
CODE = "test123"


def req(path, method="GET", payload=None, code=CODE):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if code:
        r.add_header("X-Access-Code", code)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


print("1. healthz        :", urllib.request.urlopen(BASE + "/healthz", timeout=10).status)
print("2. no code        :", req("/api/schedule", code=None))
print("3. wrong code     :", req("/api/schedule", code="wrong"))

st, body = req("/api/schedule", "PUT", {"items": [
    {"name": "Амоксициллин", "time": "09:00", "doses": 2, "days": [0,1,2,3,4,5,6], "enabled": True},
    {"name": "Витамин D", "time": "21:30", "doses": 1, "days": [0,2,4], "enabled": True},
]})
print("4. PUT valid      :", st, body)

st, body = req("/api/schedule")
print("5. GET back       :", st, json.dumps(body, ensure_ascii=False))
assert len(body["items"]) == 2, body
assert body["items"][0]["time"] == "09:00"
assert body["items"][0]["doses"] == 2
assert body["items"][1]["days"] == [0, 2, 4]

st, body = req("/api/schedule", "PUT", {"items": [
    {"name": "", "time": "09:00"},
    {"name": "BadTime", "time": "25:99"},
    {"name": "TooMany", "time": "07:15", "doses": 99},
    {"name": "Normal", "time": "08:45", "doses": 3},
]})
print("6. PUT filtered   :", st, json.dumps(body, ensure_ascii=False))
# empty name and invalid time are dropped; an oversized dose count is clamped to 20
names = [i["name"] for i in body["items"]]
assert "BadTime" not in names and "" not in names, body
assert names == ["TooMany", "Normal"], body
assert body["items"][0]["doses"] == 20, body
assert body["items"][1]["doses"] == 3, body

st, body = req("/api/schedule", "GET")
print("7. final state    :", st, json.dumps(body, ensure_ascii=False))
print("\nALL LOCAL API TESTS PASSED")
