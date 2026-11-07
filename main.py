from flask import Flask, jsonify, render_template, request, Response, g
import accounts
from accounts import refresh_session
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import os
import uuid
import json
import re
import subprocess
import tempfile
import time
import websocket
from werkzeug.utils import secure_filename
from pydub import AudioSegment


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ffmpeg_path = os.path.join(BASE_DIR, "ffmpeg.exe")


AudioSegment.converter = ffmpeg_path


os.environ["PATH"] += os.pathsep + BASE_DIR


app = Flask(__name__)

API_URL = "https://pin.apiblink.ru/api/map/markers"
ONLINE_URL = "https://pin.apiblink.ru/api/online"


BASE_HEADERS = {
    "User-Agent": "Blink/1.36 227; com.blinkmap Android/32",
    "Connection": "keep-alive",
    "Accept": "*/*",
    "Accept-Encoding": "gzip"
}


LOGS_DIR = "logs"
if not os.path.exists(LOGS_DIR):
    os.makedirs(LOGS_DIR)

def safe_jsonify(data, status=200):

    json_str = data if isinstance(data, str) else json.dumps(data)
    json_str = re.sub(r'("\w*id"\s*:\s*)(\d{15,})', r'\1"\2"', json_str)
    return Response(json_str, status=status, mimetype='application/json')

def export_to_txt(data):
    now = datetime.now()
    filename = os.path.join(LOGS_DIR, f"{now.strftime('%Y-%m-%d')}.txt")
    
    entry = [f"--- Updated successfully: {now.strftime('%Y-%m-%d %H:%M:%S')} ---"]
    markers = data.get("markers", [])
    if data.get("my_marker"):
        markers = [data["my_marker"]] + markers

    for m in markers:
        acc = m.get("account", {})
        geo = m.get("geo", {})
        username = acc.get("username", "Unknown")
        user_id = acc.get("id", "N/A")
        lat = geo.get("latitude", 0)
        lon = geo.get("longitude", 0)
        google_maps_link = f"https://www.google.com/maps?q={lat},{lon}"
        line = (f"ID: {user_id} | User: {username} | Coordinates: {lat}, {lon} | "
                f"Battery: {geo.get('charge', '?')}% | Map: {google_maps_link}")
        entry.append(line)
    
    entry.append("\n")
    new_content = "\n".join(entry)

    existing_content = ""
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            existing_content = f.read()

    with open(filename, "w", encoding="utf-8") as f:
        f.write(new_content + existing_content)

def api_request(method, url, *, account=None, **kwargs):
    if account is None:
        account = g.account
    if not account["session"]: refresh_session(account)

    headers = {**BASE_HEADERS, **kwargs.pop("headers", {}), "authorization": account["session"]}
    r = requests.request(method, url, headers=headers, **kwargs)

    if r.status_code == 401:
        print("⚠️ Session expired, refreshing...")
        if refresh_session(account):
            for _, stream, *_ in (kwargs.get("files") or {}).values():
                stream.seek(0)
            headers["authorization"] = account["session"]
            r = requests.request(method, url, headers=headers, **kwargs)
    return r

def ts_to_date(ts):
    if not ts: return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def convert_audio_for_api(input_path, output_path):
    try:
        audio = AudioSegment.from_file(input_path)
        duration_sec = len(audio) / 1000.0
        audio.export(output_path, format="adts")
        return True, duration_sec
    except Exception as e:
        print(f"Audio conversion failed: {e}")
        return False, 0.0

def process_video_for_orb(input_path, output_path):


    try:
        try:
            audio = AudioSegment.from_file(input_path)
            duration_sec = min(len(audio) / 1000.0, 59.0)
        except:
            duration_sec = 10.0 


        filter_str = "scale='max(720,iw*720/ih)':'max(720,ih*720/iw)',crop=720:720"

        command = [
            ffmpeg_path,
            "-i", input_path,
            "-t", "59",
            "-vf", filter_str,
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level", "4.1",
            "-refs", "1",
            "-x264-params", "cabac=1",
            "-color_range", "pc",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ac", "1",
            "-ar", "48000",
            "-b:a", "128k",
            "-b:v", "2M",
            "-movflags", "+faststart",
            "-y",
            output_path
        ]
        
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True, duration_sec
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
        print(f"Video conversion failed (ffmpeg):\n{error_msg}")
        return False, 0.0
    except Exception as e:
        print(f"Video message processing failed: {e}")
        return False, 0.0

@app.route("/")
def index():
    return render_template("index.html", active_id=g.account["id"] if g.account else "")
    
@app.route("/online")
def online():
    r = api_request("GET", ONLINE_URL, headers={"If-Modified-Since": datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")})
    try:
        data = r.json()
    except:
        data = []
    return safe_jsonify(data)

@app.get("/marker-tail")
def marker_tail():
    slug = request.args.get("slug", "").strip()
    if not slug or len(slug) > 256:
        return jsonify(error="Invalid marker tail."), 400
    try:
        response = api_request("GET", accounts.BASE + "/customization/asset",
                               params={"type": "marker_tail", "slug": slug, "res": "spritesheet"},
                               headers={"X-User-Agent": "web_client (client 0.1.118)",
                                        "Referer": "https://web.blinkmap.com/"}, timeout=(5, 15))
        if not response.ok:
            return jsonify(error="Marker tail unavailable."), response.status_code
        return jsonify(response.json())
    except (requests.RequestException, ValueError):
        return jsonify(error="Could not load marker tail."), 502


online_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="map-online")


@app.route("/markers")
def markers():
    online_result = online_workers.submit(api_request,"GET",ONLINE_URL,account=g.account.copy(),timeout=(5,10))
    r = api_request("POST", API_URL, timeout=(8,25))
    status_code = r.status_code
    try:
        data = r.json()
    except:
        data = {}

    online_map = {}
    try:
        online_resp = online_result.result()
        for o in online_resp.json():
            online_map[str(o["account_id"])] = {"online": o.get("online", False), "timestamp": o.get("timestamp", 0)}
    except:
        pass

    for m in data.get("markers", []):
        geo = m.get("geo", {})
        acc = m.get("account", {})
        uid = str(acc.get("id"))

        if uid in online_map:
            acc["online_now"] = online_map[uid]["online"]
            acc["online_ts"] = ts_to_date(online_map[uid]["timestamp"])
        else:
            acc["online_now"] = False
            acc["online_ts"] = ""

        geo["last_update"] = ts_to_date(geo.get("last_update_ts"))
        geo["first_entry"] = ts_to_date(geo.get("first_entry_ts"))
        acc["last_online"] = ts_to_date(acc.get("last_online_ts"))

    if data.get("my_marker"):
        geo = data["my_marker"].get("geo", {})
        acc = data["my_marker"].get("account", {})
        geo["last_update"] = ts_to_date(geo.get("last_update_ts"))
        geo["first_entry"] = ts_to_date(geo.get("first_entry_ts"))
        acc["last_online"] = ts_to_date(acc.get("last_online_ts"))
        
        if request.args.get("export") == "true" and status_code == 200:
            export_to_txt(data)

    return safe_jsonify({
        "status": status_code,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": data
    })

@app.route("/friends/<user_id>")
def get_friends(user_id):
    url = f"https://pin.apiblink.ru/api/account/{user_id}/friends?limit=1000&offset=0"
    r = api_request("GET", url)
    return safe_jsonify(r.text, r.status_code)

@app.route("/send_sticker/<user_id>", methods=["POST"])
def send_sticker(user_id):
    body = request.json
    url = f"https://pin.apiblink.ru/api/messenger/{user_id}/audio-stickers/send"
    r = api_request("POST", url, headers={"Content-Type": "application/json"}, json=body)
    return safe_jsonify(r.text, r.status_code)

@app.route("/refresh_session", methods=["POST"])
def manual_refresh():
    ok = refresh_session()
    return jsonify({"success": ok})

@app.route("/send_steps", methods=["POST"])
def send_steps():
    body = request.json
    steps = body.get("steps")
    if not steps: return jsonify({"ok": False, "error": "No steps provided"})
    now = datetime.now()
    payload = {
        "stats": [{"steps": int(steps), "date": int(now.timestamp()), "time": now.isoformat()}],
        "data_source": "gms"
    }
    url = "https://pin.apiblink.ru/api/step/stats"
    r = api_request("POST", url, headers={"Content-Type": "application/json"}, json=payload)
    return safe_jsonify({"status": r.status_code, "response": r.json() if r.status_code == 200 else r.text})

@app.route("/steps_top")
def steps_top():
    period = request.args.get("period", "day")
    url = f"https://pin.apiblink.ru/api/step/stats?period={period}"
    r = api_request("GET", url)
    try:
        return safe_jsonify(r.json(), r.status_code)
    except ValueError:
        return jsonify(error="Could not parse the step rankings.", raw=r.text),502

@app.route("/buzz/<user_id>", methods=["POST"])
def send_buzz(user_id):
    count = max(1, min(5, int(request.json.get("count", 1))))
    results = []
    for i in range(1, count + 1):
        url = f"https://pin.apiblink.ru/api/buzz/{user_id}"
        r = api_request("POST", url, headers={"Content-Type": "application/json; charset=utf-8"}, json={"step": i})
        try:
            res_data = r.json()
        except:
            res_data = r.text
        results.append({"step": i, "status": r.status_code, "data": res_data})
    return jsonify({"success": True, "user_id": user_id, "details": results})

@app.route("/update_geo", methods=["POST"])
def update_geo():
    data = request.json
    url = "https://pin.apiblink.ru/api/geo/android"
    payload = {
        "background": False,
        "geo": [{
            "service": 0, "provider": 0, "priority": 0,
            "latitude": float(data['lat']), "longitude": float(data['lon']),
            "altitude": float(data.get('altitude', 0.0)), "speed": float(data.get('speed', 0.0)),
            "timestamp": int(datetime.now().timestamp()), "accuracy": int(data.get('accuracy', 1)),
            "charge": int(data.get('charge', 80)), "heading": float(data.get('heading', -1.0)),
            "geohash": data['geohash'], "mock": False, "ignore_rules": False,
            "speed_accuracy_mps": -1.0, "vertical_accuracy_m": 2.0,
            "bearing_accuracy_deg": -1.0, "msl_altitude_m": -1.0, "msl_altitude_accuracy_m": -1.0
        }],
        "device": {"brand": "samsung", "model": "SM-G9810", "manufacture": "samsung", "sdk": 32}
    }
    r = api_request("POST", url, json=payload, headers={"Content-Type": "application/json"})
    return safe_jsonify(r.text, r.status_code)


@app.route("/chats", methods=["GET"])
def get_chats():
    url = "https://pin.apiblink.ru/api/messenger/v2/chats"
    params = {"limit": 20, "query": ""}
    cursor = request.args.get("before")
    if cursor:
        if not cursor.isascii() or not cursor.isdigit():
            return jsonify(error="Invalid chat list cursor"), 400
        params.update(last_message_id=cursor, limit=300)
    r = api_request("GET", url, params=params, timeout=20)
    return safe_jsonify(r.text, r.status_code)


@app.route("/send_audio/<chat_id>", methods=["POST"])
def send_audio(chat_id):
    if 'file' not in request.files:
        return jsonify({"error": "File not found"}), 400
        
    actor_id = request.form.get("actor_id", 0)
    file = request.files['file']
    
    temp_input = f"temp_in_{secure_filename(file.filename)}"
    temp_output = f"temp_out_{uuid.uuid4().hex}.aac"
    
    file.save(temp_input)
    success, duration = convert_audio_for_api(temp_input, temp_output)
    
    if not success:
        if os.path.exists(temp_input): os.remove(temp_input)
        return jsonify({"error": "Conversion failed. Check that ffmpeg.exe is in the project directory."}), 500
        
    os.remove(temp_input)

    if not g.account["session"]: refresh_session()
    headers = {"User-Agent": BASE_HEADERS["User-Agent"], "authorization": g.account["session"]}
    
    upload_url = "https://pin.apiblink.ru/api/messenger/voice"
    with open(temp_output, 'rb') as f:
        files = {'file': ('voice.aac', f, 'audio/aac')}
        upload_res = requests.post(upload_url, headers=headers, files=files)
        
    if upload_res.status_code == 404:
        upload_url = "https://pin.apiblink.ru/api/messenger/media"
        with open(temp_output, 'rb') as f:
            files = {'file': ('voice.aac', f, 'audio/aac')}
            upload_res = requests.post(upload_url, headers=headers, files=files)
            
    os.remove(temp_output)
    
    if upload_res.status_code != 200:
        return safe_jsonify({"error": f"Upload failed (status {upload_res.status_code})", "details": upload_res.text}, upload_res.status_code)
        
    media_data = upload_res.json()
    media_id = media_data.get("id")
    
    event_url = "https://pin.apiblink.ru/api/messenger/v2/event"
    payload = {
        "action_type": "send_voice_message",
        "actor_id": int(actor_id),
        "chat_id": int(chat_id),
        "request_id": str(uuid.uuid4()),
        "action_data": {
            "voice_id": media_id,
            "duration": duration
        }
    }
    
    event_res = api_request("POST", event_url, json=payload, headers={"Content-Type": "application/json"})
    return safe_jsonify(event_res.text, event_res.status_code)

def open_chat_socket(actor_id):
    if not g.account["session"] and not refresh_session():
        raise RuntimeError("Authentication failed")
    for attempt in range(2):
        try:
            return websocket.create_connection(
                f"wss://pin.apiblink.ru/client/ws/{actor_id}",
                header={"Authorization": g.account["session"],
                        "User-Agent": BASE_HEADERS["User-Agent"]},
                suppress_origin=True, timeout=40,
            )
        except websocket.WebSocketBadStatusException as exc:
            if exc.status_code != 401 or attempt or not refresh_session():
                raise


from chat import register_chat
disconnect = register_chat(app, api_request, open_chat_socket)
accounts.bind(app, disconnect)

def send_orb_event(actor_id, chat_id, media_id):

    request_id = str(uuid.uuid4())
    payload = {
        "event": "messenger_event",
        "data": {
            "request_id": request_id,
            "actor_id": actor_id,
            "chat_id": chat_id,
            "action_type": "send_orb_message",
            "action_data": {"type": "send_orb_message", "media_id": media_id},
        },
    }


    ws = open_chat_socket(actor_id)
    try:
        ws.send(json.dumps(payload))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ws.settimeout(max(0.1, deadline - time.monotonic()))
            raw = ws.recv()
            if not raw:
                raise RuntimeError("Connection closed before confirmation. Check the chat before retrying.")
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if not isinstance(data, dict) or data.get("request_id") != request_id:
                continue
            action = data.get("action_data") or {}
            if (event.get("event") == "messenger_event"
                    and data.get("action_type") == "new_orb_message"
                    and isinstance(action, dict) and action.get("message_id")):
                return data
            raise RuntimeError("The server did not confirm the video message: " +
                               json.dumps(event, ensure_ascii=False))
        raise websocket.WebSocketTimeoutException("ACK timeout")
    finally:
        ws.close()


@app.route("/send_orb/<chat_id>", methods=["POST"])
def send_orb(chat_id):
    if 'file' not in request.files:
        return jsonify({"error": "File not found"}), 400
    try:
        actor_id = int(request.form.get("actor_id", 0))
        chat_id = int(chat_id)
        if actor_id <= 0 or chat_id <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid ID for the sender or chat"}), 400

    media_id = None
    try:
        if not g.account["session"] and not refresh_session():
            return jsonify({"error": "Authentication failed"}), 401
        file = request.files['file']
        with tempfile.TemporaryDirectory(prefix="blink_orb_") as temp_dir:
            temp_input = os.path.join(temp_dir, secure_filename(file.filename or "video") or "video")
            temp_output = os.path.join(temp_dir, "converted_orb.mp4")

            temp_input = os.path.join(temp_dir, "input_" + os.path.basename(temp_input))
            file.save(temp_input)
            success, _ = process_video_for_orb(temp_input, temp_output)
            if not success:
                return jsonify({"error": "Video message processing failed. Check ffmpeg and the Python console."}), 500

            for attempt in range(2):

                with open(temp_output, 'rb') as stream:
                    upload_res = requests.post(
                        "https://pin.apiblink.ru/api/messenger/v2/orb/upload",
                        headers={"User-Agent": BASE_HEADERS["User-Agent"],
                                 "authorization": g.account["session"]},
                        files={"file": (f"orb_{uuid.uuid4().hex}.mp4", stream, "video/*")},
                        timeout=(15, 180),
                    )
                if upload_res.status_code != 401 or attempt or not refresh_session():
                    break
            if not 200 <= upload_res.status_code < 300:
                return safe_jsonify({"error": f"Video message upload failed: HTTP {upload_res.status_code}",
                                     "details": upload_res.text}, 502)
            try:
                media_id = int(upload_res.json()["orb_media"]["id"])
                if media_id <= 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                return jsonify({"error": "The upload response did not contain a valid orb_media.id"}), 502

        event = send_orb_event(actor_id, chat_id, media_id)
        return safe_jsonify({"success": True, "message_id": event["action_data"]["message_id"],
                             "event": event})
    except websocket.WebSocketTimeoutException:
        return safe_jsonify({"error": "Video message delivery was not confirmed. Check the chat before retrying.",
                             "media_id": media_id}, 504)
    except (requests.RequestException, websocket.WebSocketException, RuntimeError, OSError) as exc:
        return safe_jsonify({"error": str(exc), "media_id": media_id}, 502)

if __name__ == "__main__":
    app.run(port=5000, threaded=True)