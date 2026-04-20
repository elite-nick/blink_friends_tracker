from flask import Flask, jsonify, render_template, request
import requests
from datetime import datetime

app = Flask(__name__)

API_URL = "https://pin.apiblink.ru/api/map/markers"
ONLINE_URL = "https://pin.apiblink.ru/api/online"

PROVIDER_IDENTIFIER = "PROVIDER_IDENTIFIER"
STATIC_TOKEN = "STATIC_TOKEN"

BASE_HEADERS = {
    "User-Agent": "Blink/1.24.4 201; com.blinkmap Android/35",
    "Connection": "keep-alive",
    "Accept": "*/*",
    "Accept-Encoding": "gzip"
}

SESSION_TOKEN = None


def refresh_session():
    global SESSION_TOKEN

    url = "https://pin.apiblink.ru/api/login/token/v2"

    try:
        r = requests.post(url, json={
            "provider_identifier": PROVIDER_IDENTIFIER,
            "token": STATIC_TOKEN
        }, headers={
            "User-Agent": "okhttp/5.3.2",
            "Content-Type": "application/json"
        })

        if r.status_code == 200:
            data = r.json()
            SESSION_TOKEN = data.get("session")
            print("✅ Session updated:", SESSION_TOKEN)
            return True
        else:
            print("❌ Failed to refresh session:", r.status_code)
            return False

    except Exception as e:
        print("❌ Session error:", e)
        return False


def api_request(method, url, **kwargs):
    global SESSION_TOKEN

    if not SESSION_TOKEN:
        refresh_session()

    headers = kwargs.pop("headers", {})
    headers = {
        **BASE_HEADERS,
        **headers,
        "authorization": SESSION_TOKEN
    }

    r = requests.request(method, url, headers=headers, **kwargs)

    if r.status_code == 401:
        print("⚠️ Session expired, refreshing...")

        if refresh_session():
            headers["authorization"] = SESSION_TOKEN
            r = requests.request(method, url, headers=headers, **kwargs)

    return r


def ts_to_date(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@app.route("/")
def index():
    return render_template("index.html")
    
@app.route("/online")
def online():

    r = api_request(
        "GET",
        ONLINE_URL,
        headers={
            "If-Modified-Since": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
        }
    )

    try:
        data = r.json()
    except:
        data = []

    return jsonify(data)



@app.route("/markers")
def markers():

    r = api_request("POST", API_URL)
    status_code = r.status_code

    try:
        data = r.json()
    except:
        data = {}

    online_map = {}

    try:
        online_resp = api_request("GET", ONLINE_URL)
        online_data = online_resp.json()

        for o in online_data:
            online_map[o["account_id"]] = {
                "online": o.get("online", False),
                "timestamp": o.get("timestamp", 0)
            }

    except:
        pass

    for m in data.get("markers", []):
        geo = m.get("geo", {})
        acc = m.get("account", {})

        uid = acc.get("id")

        if uid in online_map:
            acc["online_now"] = online_map[uid]["online"]
            acc["online_ts"] = ts_to_date(
                online_map[uid]["timestamp"]
            )
        else:
            acc["online_now"] = False
            acc["online_ts"] = ""

        geo["last_update"] = ts_to_date(
            geo.get("last_update_ts")
        )

        geo["first_entry"] = ts_to_date(
            geo.get("first_entry_ts")
        )

        acc["last_online"] = ts_to_date(
            acc.get("last_online_ts")
        )

    if data.get("my_marker"):
        geo = data["my_marker"].get("geo", {})
        acc = data["my_marker"].get("account", {})

        geo["last_update"] = ts_to_date(
            geo.get("last_update_ts")
        )

        geo["first_entry"] = ts_to_date(
            geo.get("first_entry_ts")
        )

        acc["last_online"] = ts_to_date(
            acc.get("last_online_ts")
        )

    return jsonify({
        "status": status_code,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": data,
        "session": SESSION_TOKEN
    })

@app.route("/friends/<int:user_id>")
def get_friends(user_id):

    url = f"https://pin.apiblink.ru/api/account/{user_id}/friends?limit=1000&offset=0"
    r = api_request("GET", url)

    return jsonify(r.json())


@app.route("/send_sticker/<int:user_id>", methods=["POST"])
def send_sticker(user_id):

    body = request.json

    url = f"https://pin.apiblink.ru/api/messenger/{user_id}/audio-stickers/send"

    r = api_request("POST", url,
        headers={"Content-Type": "application/json"},
        json=body
    )

    return jsonify(r.json())


@app.route("/refresh_session", methods=["POST"])
def manual_refresh():
    ok = refresh_session()
    return jsonify({
        "success": ok,
        "session": SESSION_TOKEN
    })

@app.route("/send_steps", methods=["POST"])
def send_steps():

    body = request.json
    steps = body.get("steps")

    if not steps:
        return jsonify({"ok": False, "error": "No steps provided"})

    now = datetime.now()

    payload = {
        "stats": [{
            "steps": int(steps),
            "date": int(now.timestamp()),
            "time": now.isoformat()
        }],
        "data_source": "gms"
    }

    url = "https://pin.apiblink.ru/api/step/stats"

    r = api_request(
        "POST",
        url,
        headers={"Content-Type": "application/json"},
        json=payload
    )

    try:
        return jsonify({
            "status": r.status_code,
            "response": r.json()
        })
    except:
        return jsonify({
            "status": r.status_code,
            "response": r.text
        })

@app.route("/steps_top")
def steps_top():

    url = "https://pin.apiblink.ru/api/step/stats?period=day"

    r = api_request("GET", url)

    try:
        return jsonify(r.json())
    except:
        return jsonify({
            "error": "Failed to parse response",
            "raw": r.text
        })

if __name__ == "__main__":
    refresh_session()
    app.run(port=5000, debug=True)