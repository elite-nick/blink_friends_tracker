from flask import Flask, jsonify, render_template, request
import requests
from datetime import datetime

app = Flask(__name__)

API_URL = "https://pin.apiblink.ru/api/map/markers"

HEADERS = {
    "User-Agent": "Blink/1.24.3 200; com.blinkmap Android/35",
    "Connection": "keep-alive",
    "Accept": "*/*",
    "Accept-Encoding": "gzip",
    "Cookie": "COOKIE",
    "authorization": "TOKEN"
}


def ts_to_date(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/markers")
def markers():

    r = requests.post(API_URL, headers=HEADERS)
    status_code = r.status_code
    data = r.json()

    for m in data.get("markers", []):
        geo = m.get("geo", {})
        acc = m.get("account", {})

        geo["last_update"] = ts_to_date(geo.get("last_update_ts"))
        geo["first_entry"] = ts_to_date(geo.get("first_entry_ts"))
        acc["last_online"] = ts_to_date(acc.get("last_online_ts"))

    return jsonify({
        "status": status_code,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": data
    })


@app.route("/friends/<int:user_id>")
def get_friends(user_id):

    url = f"https://pin.apiblink.ru/api/account/{user_id}/friends?limit=1000&offset=0"
    r = requests.get(url, headers=HEADERS)

    return jsonify(r.json())


@app.route("/send_sticker/<int:user_id>", methods=["POST"])
def send_sticker(user_id):

    body = request.json

    url = f"https://pin.apiblink.ru/api/messenger/{user_id}/audio-stickers/send"

    r = requests.post(url, headers={
        **HEADERS,
        "Content-Type": "application/json"
    }, json=body)

    return jsonify(r.json())


if __name__ == "__main__":
    app.run(port=5000, debug=True)