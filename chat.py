
import json
import math
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from queue import Queue, Empty

import requests
import websocket
import media_store
from flask import Response, jsonify, request

API = "https://pin.apiblink.ru/api/messenger"
ARCHIVE = os.path.join(os.path.dirname(__file__), "chat_media.sqlite3")
ACK = {"send_message": "new_message", "send_audio_sticker": "new_audio_sticker",
       "add_snippet_reaction": "snippet_reaction_added", "read_message": "message_read",
       "view_oneshot_media_message": "oneshot_media_viewed",
       "send_media_message": "new_media_message", "send_oneshot_media_message": "new_oneshot_media_message"}


def positive(value):
    if not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
        raise ValueError("A positive integer is required")
    return int(value)


def wire(data):

    if isinstance(data, dict):
        return {k: wire(v) for k, v in data.items()}
    if isinstance(data, list):
        return [wire(v) for v in data]
    if isinstance(data, int) and abs(data) > 2**53 - 1:
        return str(data)
    return data


def archive(actor, chat, mid=None, media=(), *, copy=False, backfill=False, uploaded=False):
    jobs = []
    result = {}
    with closing(sqlite3.connect(ARCHIVE, timeout=15)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS media
            (actor TEXT, chat TEXT, message TEXT, attachment TEXT, data TEXT,
             PRIMARY KEY(actor,chat,message,attachment))""")
        if mid is not None:
            db.execute("BEGIN IMMEDIATE")
        for item in media:
            if not item.get("id") or not (uploaded or item.get("original") or item.get("preview")):
                continue
            key = tuple(map(str, (actor, chat, mid, item["id"])))
            old = db.execute("SELECT data FROM media WHERE actor=? AND chat=? AND message=? AND attachment=?", key).fetchone()
            merged = json.loads(old[0]) if old else {}
            merged.update({k:v for k,v in item.items() if v is not None and v != ""})
            db.execute("INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)", (*key,json.dumps(merged)))
            if copy:
                jobs.extend((key+(variant,), merged.get(variant)) for variant in ("original","preview"))
        if mid is None:
            media_store.schema(db)
            files = {(m,a,v):(i,mime) for m,a,v,i,mime in db.execute(
                "SELECT message,attachment,variant,id,mime FROM attachments WHERE actor=? AND chat=?",
                (str(actor),str(chat)))}
            for message, aid, raw in db.execute("SELECT message,attachment,data FROM media WHERE actor=? AND chat=?", (str(actor),str(chat))):
                item = json.loads(raw)
                for variant in ("original","preview"):
                    stored = files.get((message,aid,variant))
                    if stored:
                        file_id,mime = stored
                        item["local_"+variant] = f"/chats/{chat}/archive/{actor}/{file_id}"
                        if variant == "original":
                            item["local_mime"] = mime
                    elif backfill:
                        jobs.append(((str(actor),str(chat),message,aid,variant),item.get(variant)))
                result.setdefault(message, []).append(item)
    for key,url in jobs:
        media_store.queue_copy(ARCHIVE,key,url)
    return result


def keep_event(actor, chat, event):
    if str(event.get("chat_id")) != str(chat):
        return
    a = event.get("action_data") or {}
    if event.get("action_type") in ("oneshot_media_viewed", "new_oneshot_media_message"):
        archive(actor, chat, a["message_id"], a.get("medias") or a.get("media") or [], copy=event["action_type"] == "oneshot_media_viewed")


class Connection:
    def __init__(self, ws, actor, chat):
        self.ws, self.actor, self.chat = ws, actor, chat
        self.lock = threading.Lock()
        self.waiters = {}
        self.closed = False

    def command(self, kind, fields):
        rid, reply = str(uuid.uuid4()), Queue(1)
        with self.lock:
            if self.closed:
                raise RuntimeError("Chat connection closed")
            self.waiters[rid] = reply
        try:
            self.ws.send(json.dumps({"event":"messenger_event", "data":{
                "request_id":rid, "actor_id":self.actor, "chat_id":self.chat,
                "action_type":kind, "action_data":{"type":kind, **fields}}}))
            try:
                event = reply.get(timeout=30)
            except Empty:
                raise RuntimeError("No acknowledgement received. Check the chat before retrying.")
            if not event or event.get("action_type") != ACK[kind]:
                raise RuntimeError("The server did not confirm the action. Check the chat before retrying.")
            return event
        finally:
            with self.lock:
                self.waiters.pop(rid, None)

    def receive(self, event):
        keep_event(self.actor, self.chat, event)
        with self.lock:
            reply = self.waiters.pop(event.get("request_id"), None)
            if reply:
                reply.put(event)

    def close(self):
        with self.lock:
            self.closed = True
            for reply in self.waiters.values():
                reply.put(None)
            self.waiters.clear()
        self.ws.close()


def register_chat(app, api, open_socket):
    streams = {}
    from flask import Blueprint
    bp = Blueprint("chat", __name__)

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @bp.errorhandler(RuntimeError)
    @bp.errorhandler(requests.RequestException)
    @bp.errorhandler(websocket.WebSocketException)
    @bp.errorhandler(OSError)
    def failed(exc):
        return jsonify(error=str(exc)), 502

    @bp.get("/chats/<int:chat>/stream")
    def stream(chat):
        actor = positive(request.args.get("actor_id"))
        conn = Connection(open_socket(actor), actor, chat)
        key = str(uuid.uuid4())
        streams[key] = conn

        def events():
            try:
                yield f"event: ready\ndata: {key}\n\n"
                while True:
                    raw = conn.ws.recv()
                    if not raw:
                        break
                    frame = json.loads(raw)
                    data = frame.get("data") or {}
                    if frame.get("event") == "messenger_event" and isinstance(data,dict) and data.get("chat_id") == chat:
                        conn.receive(data)
                        yield "data: " + json.dumps(wire(data)) + "\n\n"
                    else:
                        yield ": heartbeat\n\n"
            except (OSError, websocket.WebSocketException, ValueError):
                pass
            finally:
                streams.pop(key, None)
                conn.close()
        return Response(events(), mimetype="text/event-stream", headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

    @bp.get("/chats/<int:chat>/history")
    def history(chat):
        actor = positive(request.args["actor_id"]) if "actor_id" in request.args else None
        params = {"limit_bottom":60}
        if request.args.get("before"):
            params = {"last_message_id":positive(request.args["before"]), "limit_top":50}
        elif request.args.get("after"):
            params.update(last_message_id=positive(request.args["after"]),limit_top=0)
        r = api("GET", f"{API}/v2/{chat}/history", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or []
        if actor:
            for m in items:
                if m.get("oneshot_media"):
                    archive(actor,chat,m["id"],m["oneshot_media"].get("media") or [],copy=bool(m["oneshot_media"].get("viewed")))
            data["saved_media"] = archive(actor,chat)
        return jsonify(wire(data))

    @bp.get("/chats/<int:chat>/archive/<int:actor>/<int:file_id>")
    def archive_file(chat, actor, file_id):
        return media_store.local_file(ARCHIVE,actor,chat,file_id)

    @bp.get("/chats/<int:chat>/archive")
    def archive_list(chat):
        actor = positive(request.args.get("actor_id"))
        return jsonify(wire(archive(actor,chat,backfill=request.args.get("retry","1") == "1")))

    @bp.get("/chats/<int:chat>/patch")
    def patch(chat):
        actor = positive(request.args["actor_id"])
        params = {k:positive(request.args[k]) for k in ("first_message_id","last_message_id","last_message_version")}
        r = api("GET", f"{API}/v2/patch", params={"chat_id":chat, **params}, timeout=20)
        r.raise_for_status()
        data = r.json()
        for event in data.get("events") or []:
            keep_event(actor,chat,event)
        data["saved_media"] = archive(actor,chat)
        return jsonify(wire(data))

    @bp.get("/chats/<int:chat>/<int:mid>/reaction-preview")
    def preview(chat, mid):
        r = api("GET", f"{API}/v2/{chat}/{mid}/reactions/preview", timeout=20)
        r.raise_for_status()
        return jsonify(wire(r.json()))

    @bp.get("/chat_catalog/<kind>")
    def catalog(kind):
        paths = {"quick":"snippets/quick-reactions", "all":"snippets", "stickers":"audio-stickers"}
        if kind not in paths:
            return jsonify(error="Unknown catalog"), 404
        r = api("GET", f"{API}/v2/{paths[kind]}", params={"format":"ogg"} if kind=="stickers" else {}, timeout=20)
        r.raise_for_status()
        return jsonify(wire(r.json()))

    @bp.post("/chats/<int:chat>/command")
    @bp.post("/send_text/<int:chat>")
    def command(chat):
        form = request.form if request.files else (request.get_json(silent=True) or {})
        kind = "send_message" if request.path.startswith("/send_text/") else form.get("kind")
        actor = positive(form.get("actor_id"))
        conn = streams.get(form.get("stream"))
        if not conn and kind == "send_message" and not form.get("stream"):

            ws = open_socket(actor)
            conn = Connection(ws,actor,chat)
            def receive_ack():
                try:
                    while not conn.closed:
                        raw = ws.recv()
                        if not raw: break
                        frame = json.loads(raw)
                        if frame.get("event") == "messenger_event":
                            conn.receive(frame["data"])
                except (OSError, ValueError, websocket.WebSocketException):
                    pass
                finally:
                    conn.close()
            threading.Thread(target=receive_ack, daemon=True).start()
        if not conn or (conn.actor,conn.chat) != (actor,chat):
            return jsonify(error="Chat is reconnecting. Try again once connected."),409
        try:
            if kind == "send_message":
                scale = float(form.get("font_scale", 1))
                if not math.isfinite(scale) or scale <= 0 or not str(form.get("text") or "").strip():
                    raise ValueError("Enter text and a positive font scale")
                fields = {"text":form["text"],"font_settings":{"font_id":0,"font_scale":scale}}
            elif kind == "send_audio_sticker":
                fields = {"stickers":[{"sticker_id":positive(form.get("sticker_id")), "count":positive(form.get("count",1))}]}
            elif kind in ("read_message","view_oneshot_media_message","add_snippet_reaction"):
                fields = {"message_id":positive(form.get("message_id"))}
                if kind == "add_snippet_reaction":
                    fields["items"] = [{"snippet_id":positive(form.get("snippet_id")), "count":positive(form.get("count",1))}]
            elif kind in ("send_media_message","send_oneshot_media_message"):
                file = request.files.get("file")
                if not file or file.mimetype.split("/")[0] not in ("image","video"):
                    raise ValueError("Select an image or a video")
                r = api("POST", API+"/media",files={"file":(file.filename,file.stream,file.mimetype)},timeout=(15,180))
                r.raise_for_status()
                fields = {"media_ids":[positive(r.json().get("id"))]}
            else:
                raise ValueError("Unknown action")
            ack = conn.command(kind,fields)
            if kind == "send_oneshot_media_message":
                mid = ack["action_data"]["message_id"]
                item = r.json()
                key = tuple(map(str,(actor,chat,mid,item["id"])))
                try:
                    media_store.save_file(ARCHIVE,key+("original",),file.stream)
                    archive(actor,chat,mid,[item],copy=True,uploaded=True)
                except (sqlite3.Error,OSError):
                    app.logger.exception("Could not save a copy of the sent attachment")
            return jsonify(wire(ack))
        finally:
            if not form.get("stream"):
                conn.close()

    app.register_blueprint(bp)

    def disconnect(active_actor):
        for key,conn in list(streams.items()):
            if str(conn.actor) != active_actor:
                streams.pop(key,None)
                conn.close()
    return disconnect
