"""Каталог мемов.

python memes.py          - пересканировать папки, сделать превью, обновить каталог
python memes.py serve    - то же самое + запустить локальный сервер и открыть каталог в браузере
                           (в этом режиме работают кнопки "показать в проводнике",
                           редактирование тегов, перенос в другую папку и пересканирование)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
THUMBS = os.path.join(HERE, "thumbs")
TAGS_FILE = os.path.join(HERE, "tags.json")
CACHE_FILE = os.path.join(HERE, "cache.json")
DATA_FILE = os.path.join(HERE, "memes.js")
HTML_NAME = "Каталог мемов.html"
PORT = 8765

VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".gif"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
SKIP_DIRS = {"_catalog"}
INBOX = "00 Новые (разобрать)"

MAT = re.compile(r"ху[йяеёи]|пизд|бля|[её]б[аеуёлн]|ёб|сук[аи]|пид[оа]р|нахер|дроч|говн|дерьм|жоп|долбо|залуп|обосра|писюн", re.I)
LOCK = threading.Lock()
NOWIN = 0x08000000 if os.name == "nt" else 0


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def probe(path):
    if not FFMPEG:
        return {}
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    info = {}
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if m:
        info["dur"] = round(int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), 2)
    m = re.search(r"Video: .*?(\d{2,5})x(\d{2,5})", r.stderr)
    if m:
        info["w"], info["h"] = int(m[1]), int(m[2])
    return info


def make_thumb(path, dur, out):
    if not FFMPEG:
        return False
    t = min(max(dur * 0.3, 0), 4) if dur else 0.5
    for ss in (t, 0):
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{ss:.2f}", "-i", path,
                        "-frames:v", "1", "-vf", "scale=400:-2", "-q:v", "5", out],
                       capture_output=True, creationflags=NOWIN)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return True
    return False


JUNK = re.compile(r"субтитр|корректор|музык|подписывайтесь|в комментариях|продолжение следует|^пою|^взрыв$|\.com", re.I)


def clean_segments(segs):
    """Убирает типичные галлюцинации Whisper на тишине и музыке."""
    out = []
    for s in segs:
        t = (s.get("t") or "").strip()
        if not t or s.get("nsp", 0) >= 0.5 or JUNK.search(t):
            continue
        if re.fullmatch(r"(?:\W*\b(\w+)\b\W*)(?:\1\W*){4,}", t, re.I) and len(t) < 40:
            continue  # "if if if if"
        out.append(t)
    # один и тот же короткий кусок много раз подряд - тоже галлюцинация
    out = [t for t in out if len(t) > 6 or out.count(t) < 4]
    return " ".join(out)


_WHISPER = None


def transcribe(full):
    """Распознаёт речь, если установлен faster-whisper. Возвращает None, если распознавать нечем."""
    global _WHISPER
    if not FFMPEG:
        return None
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except Exception:
        return None
    if _WHISPER is None:
        try:
            _WHISPER = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        except Exception:
            _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    r = subprocess.run([FFMPEG, "-v", "error", "-i", full, "-t", "120", "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "f32le", "-"], capture_output=True, creationflags=NOWIN)
    audio = np.frombuffer(r.stdout, np.float32)
    if len(audio) < 1600:
        return ""
    def run():
        segs, _ = _WHISPER.transcribe(audio, language="ru", beam_size=5, condition_on_previous_text=False)
        return [{"t": s.text, "nsp": s.no_speech_prob} for s in segs]
    try:
        segs = run()
    except Exception:
        # видеокарта не завелась (нет библиотек CUDA) - считаем на процессоре
        _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
        segs = run()
    return clean_segments(segs)


def auto_tags(rel, name, info, kind):
    tags = []
    if MAT.search(name):
        tags.append("мат")
    low = name.lower()
    if "[gs" in low or "гринскрин" in low:
        tags.append("гринскрин")
    if "фон" in low and "[" in low:
        tags.append("фон")
    if "футаж" in low:
        tags.append("футаж")
    if kind == "audio":
        tags.append("только звук")
    d = info.get("dur") or 0
    if d > 20:
        tags.append("длинный")
    elif 0 < d <= 3:
        tags.append("короткий")
    if rel.startswith(INBOX):
        tags.append("не разобрано")
    return tags


def walk_media():
    for d, dirs, files in os.walk(ROOT):
        rel_d = os.path.relpath(d, ROOT)
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXT or ext in AUDIO_EXT:
                rel = f if rel_d == "." else os.path.join(rel_d, f)
                yield rel.replace(os.sep, "/"), ext


def scan(log=print):
    os.makedirs(THUMBS, exist_ok=True)
    cache = load_json(CACHE_FILE, {})
    tags_db = load_json(TAGS_FILE, {})
    files = list(walk_media())
    today = date.today().isoformat()

    # 1) метаданные + превью (с кэшем, параллельно)
    stats = {}
    todo = []
    for rel, ext in files:
        full = os.path.join(ROOT, rel)
        st = os.stat(full)
        stats[rel] = st
        c = cache.get(rel)
        if not c or c.get("size") != st.st_size or c.get("mtime") != int(st.st_mtime):
            todo.append((rel, ext))

    def work(item):
        rel, ext = item
        full = os.path.join(ROOT, rel)
        st = stats[rel]
        info = probe(full)
        info.update(size=st.st_size, mtime=int(st.st_mtime))
        if ext in VIDEO_EXT:
            th = hashlib.md5(f"{rel}|{st.st_size}".encode()).hexdigest()[:16] + ".jpg"
            if make_thumb(full, info.get("dur"), os.path.join(THUMBS, th)):
                info["thumb"] = th
        return rel, info

    if todo:
        log(f"Новых или изменённых файлов: {len(todo)}, делаю превью...")
        with ThreadPoolExecutor(max_workers=6) as ex:
            for i, (rel, info) in enumerate(ex.map(work, todo), 1):
                cache[rel] = info
                if i % 20 == 0 or i == len(todo):
                    log(f"  {i}/{len(todo)}")
    present = {rel for rel, _ in files}
    for rel in list(cache):
        if rel not in present:
            del cache[rel]
    used_thumbs = {c.get("thumb") for c in cache.values()}
    for t in os.listdir(THUMBS):
        if t not in used_thumbs:
            os.remove(os.path.join(THUMBS, t))
    save_json(CACHE_FILE, cache)

    # 2) теги: переносим их за файлом, если его переименовали или переместили
    orphans = {k: v for k, v in tags_db.items() if k not in present}
    new_files = [rel for rel in present if rel not in tags_db]
    added = []
    for rel in new_files:
        size = stats[rel].st_size
        base = os.path.basename(rel)
        match = None
        for k in orphans:
            if os.path.basename(k) == base:
                match = k
                break
        if not match:
            same_size = [k for k in orphans if orphans[k].get("size") == size]
            if len(same_size) == 1:
                match = same_size[0]
        if match:
            tags_db[rel] = orphans.pop(match)
        else:
            tags_db[rel] = {"tags": [], "added": today}
            added.append(rel)
    for k in orphans:
        tags_db.pop(k, None)
    for rel in present:
        tags_db[rel]["size"] = stats[rel].st_size
    save_json(TAGS_FILE, dict(sorted(tags_db.items())))

    # 2б) расшифровка речи для файлов, где её ещё нет
    need = sorted(rel for rel in present if "text" not in tags_db[rel])
    if need:
        log(f"Распознаю речь в {len(need)} файлах...")
        for i, rel in enumerate(need, 1):
            try:
                t = transcribe(os.path.join(ROOT, rel))
            except Exception as e:
                log(f"  не вышло: {rel} ({e})")
                t = None
            if t is None:
                log("  faster-whisper не установлен, пропускаю (python -m pip install faster-whisper)")
                break
            tags_db[rel]["text"] = t
            log(f"  {i}/{len(need)} {os.path.basename(rel)}: {t[:60] or '(без речи)'}")
        save_json(TAGS_FILE, dict(sorted(tags_db.items())))

    # 3) пары видео + отдельная аудиодорожка с тем же именем
    by_stem = {}
    for rel, ext in files:
        by_stem.setdefault(os.path.splitext(rel)[0].lower(), []).append((rel, ext))

    items = []
    for rel, ext in files:
        kind = "video" if ext in VIDEO_EXT else "audio"
        siblings = by_stem[os.path.splitext(rel)[0].lower()]
        if kind == "audio" and any(e in VIDEO_EXT for _, e in siblings):
            continue  # будет показан как дорожка к видео
        folder = rel.split("/")[0] if "/" in rel else "Без папки"
        name = os.path.splitext(os.path.basename(rel))[0]
        info = cache.get(rel, {})
        it = {
            "p": rel, "name": name, "cat": folder, "kind": kind,
            "tags": tags_db[rel].get("tags", []), "auto": auto_tags(rel, name, info, kind),
            "added": tags_db[rel].get("added", today),
            "text": tags_db[rel].get("text", ""),
            "dur": info.get("dur"), "w": info.get("w"), "h": info.get("h"),
            "thumb": info.get("thumb"), "size": info.get("size"),
        }
        audio = [r for r, e in siblings if e in AUDIO_EXT and kind == "video"]
        if audio:
            it["audio"] = audio[0]
            it["auto"].append("есть аудиодорожка")
        items.append(it)

    folders = sorted({d for d in os.listdir(ROOT)
                      if os.path.isdir(os.path.join(ROOT, d)) and d not in SKIP_DIRS and not d.startswith(".")})
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "root": ROOT,
            "folders": folders, "items": items}
    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        fh.write("window.MEMES = ")
        json.dump(data, fh, ensure_ascii=False)
        fh.write(";\n")
    log(f"В каталоге: {len(items)} мемов. Новых: {len(added)}")
    for a in added:
        log(f"  + {a}")
    return data


def to_recycle_bin(paths):
    """Перемещает файлы в Корзину Windows (их можно восстановить)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom="\0".join(paths) + "\0\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise OSError(f"Не удалось удалить (код {rc})")


def safe_path(rel):
    full = os.path.normpath(os.path.join(ROOT, rel))
    if not full.startswith(os.path.normpath(ROOT) + os.sep):
        raise ValueError("bad path")
    return full


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store" if self.path.startswith(("/api", "/_catalog/memes.js")) else "max-age=60")
        super().end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/" + urllib.parse.quote(HTML_NAME))
            self.end_headers()
            return
        if self.path == "/api/ping":
            return self.send_json({"ok": True})
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()
        # поддержка Range, чтобы в плеере работала перемотка
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        start = int(m[1]) if m and m[1] else 0
        end = int(m[2]) if m and m[2] else size - 1
        if m and not m[1] and m[2]:
            start, end = size - int(m[2]), size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            with LOCK:
                if self.path == "/api/rescan":
                    return self.send_json(scan(log=lambda *_: None))
                if self.path == "/api/reveal":
                    full = safe_path(req["p"])
                    subprocess.Popen(["explorer", "/select,", full])
                    return self.send_json({"ok": True})
                if self.path == "/api/open":
                    os.startfile(safe_path(req["p"]))
                    return self.send_json({"ok": True})
                if self.path == "/api/tags":
                    db = load_json(TAGS_FILE, {})
                    tags = sorted({t.strip().lower() for t in req["tags"] if t.strip()})
                    db.setdefault(req["p"], {"added": date.today().isoformat()})["tags"] = tags
                    save_json(TAGS_FILE, db)
                    return self.send_json(scan(log=lambda *_: None))
                if self.path == "/api/delete":
                    src = safe_path(req["p"])
                    paths = [src] + [os.path.splitext(src)[0] + e for e in AUDIO_EXT
                                     if os.path.exists(os.path.splitext(src)[0] + e)
                                     and os.path.normcase(os.path.splitext(src)[0] + e) != os.path.normcase(src)]
                    to_recycle_bin(paths)
                    return self.send_json(scan(log=lambda *_: None))
                if self.path == "/api/move":
                    src = safe_path(req["p"])
                    folder = req["folder"].strip()
                    name = req.get("name", "").strip() or os.path.splitext(os.path.basename(src))[0]
                    if re.search(r'[<>:"/\\|?*]', folder + name) or not folder:
                        return self.send_json({"error": "Недопустимые символы в имени"}, 400)
                    ext = os.path.splitext(src)[1]
                    dst = safe_path(f"{folder}/{name}{ext}")
                    if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
                        return self.send_json({"error": "Файл с таким именем уже есть"}, 400)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    pairs = [(src, dst)]
                    for e in AUDIO_EXT:  # тащим за видео его аудиодорожку
                        a = os.path.splitext(src)[0] + e
                        if os.path.exists(a) and os.path.normcase(a) != os.path.normcase(src):
                            pairs.append((a, os.path.splitext(dst)[0] + e))
                    db = load_json(TAGS_FILE, {})
                    for s, d in pairs:
                        os.replace(s, d)
                        rs = os.path.relpath(s, ROOT).replace(os.sep, "/")
                        rd = os.path.relpath(d, ROOT).replace(os.sep, "/")
                        if rs in db:
                            db[rd] = db.pop(rs)
                    save_json(TAGS_FILE, db)
                    data = scan(log=lambda *_: None)
                    data["moved"] = os.path.relpath(dst, ROOT).replace(os.sep, "/")
                    return self.send_json(data)
            self.send_json({"error": "unknown"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


def serve():
    url = f"http://127.0.0.1:{PORT}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("Каталог уже запущен, открываю его в браузере.")
        webbrowser.open(url)
        return
    print(f"Каталог открыт: {url}")
    print("Не закрывай это окно, пока пользуешься каталогом (Ctrl+C - выход).")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not FFMPEG:
        print("ffmpeg не найден: превью и длительности не будут созданы.")
        print("Установи его командой:  python -m pip install imageio-ffmpeg")
    scan()
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
