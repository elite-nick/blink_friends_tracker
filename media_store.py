
import logging
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from urllib.parse import urlsplit

import requests
from flask import Response, abort, request

CHUNK = 256 * 1024
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="oneshot")
_lock = threading.Lock()
_pending = set()


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY, actor TEXT, chat TEXT, message TEXT, attachment TEXT,
        variant TEXT, mime TEXT, size INTEGER,
        UNIQUE(actor,chat,message,attachment,variant))""")
    db.execute("""CREATE TABLE IF NOT EXISTS attachment_chunks (
        file INTEGER, part INTEGER, data BLOB NOT NULL,
        PRIMARY KEY(file,part)) WITHOUT ROWID""")


def media_type(head):
    if head.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if head.startswith((b'GIF87a', b'GIF89a')): return 'image/gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP': return 'image/webp'
    if head.startswith(b'\x1a\x45\xdf\xa3'): return 'video/webm'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'avif', b'avis'): return 'image/avif'
        if brand in (b'heic', b'heix', b'mif1'): return 'image/heic'
        return 'video/quicktime' if brand == b'qt  ' else 'video/mp4'
    return None


def save_file(path, key, stream):
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    mime = media_type(stream.read(32))
    if not mime:
        return False
    stream.seek(0)
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        schema(db)
        row = db.execute("""INSERT OR IGNORE INTO attachments
            (actor,chat,message,attachment,variant,mime,size) VALUES(?,?,?,?,?,?,?)""", (*key,mime,size))
        if row.rowcount:
            chunks = iter(lambda: stream.read(CHUNK), b'')
            db.executemany("INSERT INTO attachment_chunks VALUES(?,?,?)",
                           ((row.lastrowid,i,data) for i,data in enumerate(chunks)))
    return True


def queue_copy(path, key, url):
    if not isinstance(url,str):
        return
    host = urlsplit(url)
    if host.scheme != 'https' or host.netloc not in ('chats.cdn.apiblink.ru','chats.cdn.hexawall.ru'):
        return
    job = (path,key)
    with _lock:
        if job in _pending:
            return
        _pending.add(job)

    def download():
        try:
            with closing(sqlite3.connect(path,timeout=30)) as db:
                schema(db)
                if db.execute("""SELECT 1 FROM attachments WHERE
                    actor=? AND chat=? AND message=? AND attachment=? AND variant=?""",key).fetchone():
                    return
            with requests.get(url,stream=True,allow_redirects=False,timeout=(5,30),
                              headers={'Accept-Encoding':'identity'}) as res:
                if res.status_code != 200:
                    return
                with tempfile.TemporaryFile() as tmp:
                    for block in res.iter_content(CHUNK):
                        tmp.write(block)
                    length = res.headers.get('Content-Length')
                    if length and not res.headers.get('Content-Encoding') and int(length) != tmp.tell():
                        return
                    save_file(path,key,tmp)
        except (OSError, ValueError, sqlite3.Error, requests.RequestException) as exc:
            logging.warning("Could not archive one-time media (%s)",type(exc).__name__)
        finally:
            with _lock:
                _pending.discard(job)
    _pool.submit(download)


def local_file(path, actor, chat, file_id):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        schema(db)
        row=db.execute("SELECT size,mime FROM attachments WHERE id=? AND actor=? AND chat=?",
                       (file_id,str(actor),str(chat))).fetchone()
    if not row:
        abort(404)
    size,mime=row
    start,end=0,size
    partial='Range' in request.headers
    if partial:
        wanted=request.range
        span=wanted.range_for_length(size) if wanted else None
        if not span:
            return Response(status=416,headers={'Content-Range':f'bytes */{size}'})
        start,end=span

    def contents():
        with closing(sqlite3.connect(path,timeout=15)) as db:
            parts=db.execute("""SELECT part,data FROM attachment_chunks
                WHERE file=? AND part BETWEEN ? AND ? ORDER BY part""",(file_id,start//CHUNK,(end-1)//CHUNK))
            for n,block in parts:
                yield block[max(0,start-n*CHUNK):end-n*CHUNK]

    headers={'Accept-Ranges':'bytes','Content-Length':str(end-start),'X-Content-Type-Options':'nosniff'}
    if partial:
        headers['Content-Range']=f'bytes {start}-{end-1}/{size}'
    return Response(contents(),status=206 if partial else 200,headers=headers,mimetype=mime)
