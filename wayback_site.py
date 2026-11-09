#!/usr/bin/env python3
"""wayback-site-saver: сохраняет сайт из Internet Archive (Wayback Machine) в папку
и показывает его офлайн через встроенный локальный сервер.

Только стандартная библиотека, Python 3.9+.

Примеры:
  python wayback_site.py example.com --list               # какие разделы есть в архиве
  python wayback_site.py example.com --to 20240101        # скачать снимки не позже даты
  python wayback_site.py example.com --include '^/docs/'  # только раздел /docs/
  python wayback_site.py example.com --serve              # открыть копию в браузере

Скачивание можно прерывать (Ctrl+C) и запускать заново: готовые файлы пропускаются.
"""
import argparse
import functools
import hashlib
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.0.0"

CDX = "https://web.archive.org/cdx/search/cdx"
# id_ — исходные байты файла без тулбара Wayback и без переписывания ссылок
SNAPSHOT = "https://web.archive.org/web/{ts}id_/{url}"
UA = f"wayback-site-saver/{__version__} (personal offline copy tool)"

PAGE_EXT = (".html", ".htm", ".shtml", ".php", ".asp", ".aspx", ".jsp")
REWRITE_EXT = set(PAGE_EXT) | {".css"}
TEXT_TYPES = {ext: "text/html" for ext in PAGE_EXT}
TEXT_TYPES[".css"] = "text/css"
# стили и скрипты берём даже вне --include: без них страницы выглядят сломанными
ASSET_MIME = ("text/css", "application/javascript", "text/javascript", "application/x-javascript")
RETRY_CODES = {429, 500, 502, 503, 504}

LAST_ERROR = ""


# --------------------------------------------------------------------------- сеть

def http_get(url, retries=5, timeout=60):
    """Вернёт байты или None; причина последней ошибки остаётся в LAST_ERROR."""
    global LAST_ERROR
    delay = 5
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            LAST_ERROR = f"HTTP {e.code}"
            if e.code not in RETRY_CODES:
                return None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            LAST_ERROR = f"{type(e).__name__}: {e}"
        time.sleep(delay)
        delay = min(delay * 2, 120)
    return None


def normalize_host(value):
    """'https://WWW.Example.com/x' -> 'example.com'; кириллические домены -> punycode."""
    value = value.strip()
    if "//" not in value:
        value = "//" + value
    host = (urllib.parse.urlsplit(value).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host


def cdx_query(host, to_ts, legacy):
    """Список снимков сайта. legacy=True — упрощённый запрос: фильтр статуса на стороне
    архива и без повторных снимков (запасной вариант, если основной не проходит)."""
    global LAST_ERROR
    params = {
        "url": host + "/*",
        "output": "json",
        "collapse": "timestamp:8",
        "limit": "20000",
        "showResumeKey": "true",
    }
    if legacy:
        params["fl"] = "timestamp,original,mimetype"
        params["filter"] = "statuscode:200"
    else:
        params["fl"] = "timestamp,original,mimetype,statuscode"
    if to_ts:
        params["to"] = to_ts
    rows, resume = [], None
    while True:
        q = dict(params)
        if resume:
            q["resumeKey"] = resume
        raw = http_get(CDX + "?" + urllib.parse.urlencode(q), timeout=180)
        if raw is None:
            return None
        try:
            data = json.loads(raw) if raw.strip() else []
        except ValueError:
            LAST_ERROR = "архив вернул не JSON"
            return None
        resume = None
        if len(data) >= 2 and data[-2] == [] and len(data[-1]) == 1:
            resume = data[-1][0]
            data = data[:-2]
        for r in data:
            if not r or r[0] == "timestamp":
                continue
            if legacy:
                rows.append((r[0], r[1], r[2]))
                continue
            ts, url, mime, status = r[:4]
            # 200 — обычный снимок; "-" у warc/revisit — повтор уже сохранённого файла
            # (так архив хранит неизменившиеся картинки), его тоже можно скачать
            if status == "200" or (status == "-" and mime == "warc/revisit"):
                rows.append((ts, url, mime))
        print(f"CDX: {len(rows)} записей", file=sys.stderr)
        if not resume:
            break
    return rows


def fetch_cdx(host, to_ts):
    rows = cdx_query(host, to_ts, legacy=False)
    if rows is None:
        print(f"Основной запрос не прошёл ({LAST_ERROR}), пробую упрощённый…", file=sys.stderr)
        rows = cdx_query(host, to_ts, legacy=True)
    return rows


# --------------------------------------------------------------------------- пути и ссылки

def norm_key(url):
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host + p.path + ("?" + p.query if p.query else "")


def pick_latest(rows):
    """Для каждого URL оставляем самый свежий снимок."""
    latest = {}
    for ts, url, mime in rows:
        k = norm_key(url)
        if k not in latest or ts > latest[k][0]:
            latest[k] = (ts, url, mime)
    return latest


def _safe_part(part):
    part = re.sub(r'[<>:"|?*\\\x00-\x1f]', "_", part).rstrip(" .") or "_"
    if len(part) > 200:
        base, ext = os.path.splitext(part)
        digest = hashlib.sha1(part.encode("utf-8", "replace")).hexdigest()[:8]
        part = base[:150] + "_" + digest + ext[:20]
    return part


def local_path(url, out):
    """URL -> путь файла в папке копии (безопасный для Windows и Linux)."""
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(p.path) or "/"
    if path.endswith("/"):
        path += "index.html"
    elif "." not in path.rsplit("/", 1)[-1]:
        path += "/index.html"
    if p.query:
        q = re.sub(r"[^A-Za-z0-9._-]+", "_", p.query)[:80]
        base, ext = os.path.splitext(path)
        path = base + "__" + q + ext
    parts = [x for x in path.split("/") if x and x not in (".", "..")]
    return os.path.join(out, *[_safe_part(x) for x in parts])


def host_regex(host):
    """Абсолютные ссылки на сам сайт превращаем в корневые (/docs/...),
    чтобы копия работала через локальный сервер."""
    return re.compile(rb"(?:https?:)?//(?:www\.)?" + re.escape(host.encode("ascii"))
                      + rb"(?::\d+)?(?![\w-]|\.\w)", re.I)


# --------------------------------------------------------------------------- скачивание

def download(item, out, delay, host_re):
    ts, url, _mime = item
    dest = local_path(url, out)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return "skip", url
    raw = http_get(SNAPSHOT.format(ts=ts, url=url))
    if raw is None:
        return "fail", url
    if os.path.splitext(dest)[1].lower() in REWRITE_EXT:
        raw = host_re.sub(b"", raw)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(raw)
    except OSError:
        return "fail", url
    time.sleep(delay)
    return "ok", url


# --------------------------------------------------------------------------- сервер

def detect_charset(path, legacy_encoding="windows-1251"):
    """UTF-8, если файл целиком декодируется как UTF-8, иначе запасная кодировка."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return "utf-8"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return legacy_encoding


def make_handler(directory, legacy_encoding="windows-1251"):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def guess_type(self, path):
            # charset в заголовке важнее <meta>, так что кодировка не зависит от того,
            # что написано внутри страницы или что угадает браузер
            ext = os.path.splitext(path)[1].lower()
            if ext in TEXT_TYPES:
                return f"{TEXT_TYPES[ext]}; charset={detect_charset(path, legacy_encoding)}"
            return super().guess_type(path)

        def end_headers(self):
            # без кэша: иначе браузер может держать страницу со старым заголовком кодировки
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    return functools.partial(Handler, directory=directory)


def create_server(directory, port, legacy_encoding="windows-1251"):
    # ThreadingHTTPServer включает reuse_address: порт освобождается сразу после остановки
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(directory, legacy_encoding))


def serve(out, port, legacy_encoding):
    if not os.path.isdir(out):
        sys.exit(f"Папки {out} нет. Сначала скачай копию.")
    try:
        srv = create_server(out, port, legacy_encoding)
    except OSError as e:
        sys.exit(f"Порт {port} занят ({e.strerror}). Останови другой сервер "
                 f"или запусти на другом порту: --port {port + 1}")
    with srv:
        print(f"Кодировка определяется автоматически (UTF-8 / {legacy_encoding}), кэш браузера отключён.")
        print(f"Открой http://127.0.0.1:{port}/  (Ctrl+C — остановить)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


# --------------------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(prog="wayback_site.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("domain", nargs="?", help="домен сайта, например example.com")
    ap.add_argument("--out", help="папка копии (по умолчанию wayback_copy/<домен>)")
    ap.add_argument("--to", metavar="ГГГГММДД", help="брать снимки не позже этой даты")
    ap.add_argument("--include", default=".*", help="regex по пути URL: что скачивать (по умолчанию всё)")
    ap.add_argument("--exclude", help="regex по пути URL: что пропустить")
    ap.add_argument("--delay", type=float, default=1.0, help="пауза после каждого файла, сек")
    ap.add_argument("--workers", type=int, default=3, help="число потоков загрузки")
    ap.add_argument("--legacy-encoding", default="windows-1251",
                    help="кодировка страниц, которые не в UTF-8 (по умолчанию windows-1251)")
    ap.add_argument("--list", action="store_true", help="показать разделы сайта в архиве и выйти")
    ap.add_argument("--serve", action="store_true", help="показать готовую копию на локальном сервере")
    ap.add_argument("--port", type=int, default=8000, help="порт локального сервера")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    host = normalize_host(args.domain) if args.domain else ""
    out = args.out or (os.path.join("wayback_copy", host) if host else None)
    if out is None:
        ap.error("укажи домен или папку копии через --out")
    print(f"wayback-site-saver {__version__}")

    if args.serve:
        serve(out, args.port, args.legacy_encoding)
        return
    if not host:
        ap.error("нужен домен, например: wayback_site.py example.com")

    # список страниц кэшируем в папке копии: если архив потом не ответит, возьмём его
    cache = os.path.join(out, f"_cdx_{args.to or 'latest'}.json")
    rows = fetch_cdx(host, args.to)
    if rows is not None:
        if not args.list:
            os.makedirs(out, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(rows, f)
    elif os.path.exists(cache):
        print("Архив не ответил, беру список страниц, сохранённый в прошлый раз.", file=sys.stderr)
        with open(cache, encoding="utf-8") as f:
            rows = json.load(f)
    else:
        sys.exit("Не удалось получить список страниц из web.archive.org.\n"
                 f"Причина: {LAST_ERROR or 'нет ответа'}.\n"
                 "Если это 429/503 или таймаут, архив ограничил запросы: "
                 "подожди 10–15 минут и запусти ту же команду снова.")
    latest = pick_latest(rows)
    if not latest:
        sys.exit("В архиве ничего не найдено для этого домена.")

    if args.list:
        sections = Counter()
        for _ts, url, _m in latest.values():
            seg = urllib.parse.urlsplit(url).path.split("/")[1:2]
            sections["/" + (seg[0] if seg else "") + ("/" if seg and seg[0] else "")] += 1
        for name, n in sections.most_common(40):
            print(f"{n:6d}  {name}")
        return

    inc = re.compile(args.include)
    exc = re.compile(args.exclude) if args.exclude else None
    todo = []
    for item in latest.values():
        path = urllib.parse.urlsplit(item[1]).path
        if exc and exc.search(path):
            continue
        if inc.search(path) or item[2].lower().startswith(ASSET_MIME):
            todo.append(item)
    todo.sort(key=lambda x: x[1])
    print(f"К скачиванию: {len(todo)} файлов (из {len(latest)} в архиве)")
    if not todo:
        sys.exit("Под условия --include/--exclude ничего не подошло.")

    os.makedirs(out, exist_ok=True)
    host_re = host_regex(host)
    stop = threading.Event()
    stats, failed = Counter(), []

    def task(item):
        if stop.is_set():
            return "cancel", item[1]
        return download(item, out, args.delay, host_re)

    interrupted = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        try:
            for i, (status, url) in enumerate(pool.map(task, todo), 1):
                stats[status] += 1
                if status == "fail":
                    failed.append(url)
                if i % 25 == 0 or i == len(todo):
                    print(f"{i}/{len(todo)}  ok={stats['ok']} skip={stats['skip']} fail={stats['fail']}")
        except KeyboardInterrupt:
            stop.set()  # остальные задачи из очереди завершатся сразу
            interrupted = True
    if interrupted:
        print("\nОстановлено. Запусти ту же команду, чтобы продолжить: готовые файлы пропустятся.")
        sys.exit(130)

    if failed:
        with open(os.path.join(out, "_failed.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(failed))
        print(f"Не скачалось {len(failed)} файлов, список в {os.path.join(out, '_failed.txt')} — "
              "запусти ту же команду ещё раз, она докачает.")
    target = f"--out {out}" if args.out else args.domain
    print(f"Готово. Посмотреть: python {os.path.basename(sys.argv[0])} {target} --serve")


if __name__ == "__main__":
    main()
