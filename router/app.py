"""search-router: единая точка входа для агентов поверх SearXNG.

Решает три задачи, которых у SearXNG нет:

1. Каскад ярусов. SearXNG опрашивает движки категории строго одновременно
   (searx/search/__init__.py — поток на движок), понятия fallback в нём нет.
   Значит «сначала бесплатные, при неудаче платные» выразимо только снаружи.
2. Бюджет платных ключей. Метрики SearXNG живут в памяти и обнуляются при
   перезапуске, а /usage у Tavily отстаёт, поэтому счётчик ведётся здесь и
   хранится в valkey.
3. Отбраковка мусора. Пустая выдача и выдача не с тех доменов (так делал qwant
   на кириллице) одинаково означают «ярус не справился» и ведут к эскалации.

Параллельность движков внутри яруса и консенсус между ними — работа SearXNG:
он взвешивает результат по числу нашедших его движков, поэтому здесь не
дублируется.

Зависимостей нет намеренно: сборка образа не должна зависеть от сети.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng-core:8080")
VALKEY_HOST = os.environ.get("VALKEY_HOST", "valkey")
VALKEY_PORT = int(os.environ.get("VALKEY_PORT", "6379"))
ROUTER_PORT = int(os.environ.get("ROUTER_PORT", "8090"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "45"))

FREE_ENGINES = [e.strip() for e in os.environ.get(
    "FREE_ENGINES", "google,yandex,bing,duckduckgo web").split(",") if e.strip()]
PAID_ENGINES = [e.strip() for e in os.environ.get("PAID_ENGINES", "tavily").split(",") if e.strip()]

PACKS_FILE = os.environ.get("PACKS_FILE", "/app/packs.json")


def _limits():
    """Лимиты платных движков: LIMIT_<движок>_MONTH / _DAY, имя в верхнем регистре."""
    out = {}
    for engine in PAID_ENGINES:
        key = engine.upper().replace("-", "_").replace(" ", "_")
        out[engine] = {
            "month": int(os.environ.get("LIMIT_%s_MONTH" % key, "1000")),
            "day": int(os.environ.get("LIMIT_%s_DAY" % key, "50")),
        }
    return out


LIMITS = _limits()


def load_packs():
    try:
        with open(PACKS_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        # Пакеты — не обязательное условие работы: без них роутер просто не фильтрует по доменам.
        return {}


PACKS = load_packs()


class Valkey:
    """Минимальный клиент RESP: нужны только INCR, EXPIRE, GET, MGET.

    Своя реализация вместо библиотеки, чтобы образ собирался без pip и,
    значит, без сети — она в этом окружении регулярно рвётся.
    """

    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._sock = None
        self._file = None
        self._lock = threading.Lock()

    def _connect(self):
        self._sock = socket.create_connection((self._host, self._port), timeout=5)
        self._file = self._sock.makefile("rb")

    def _send(self, *args):
        payload = ["*%d\r\n" % len(args)]
        for arg in args:
            raw = str(arg).encode()
            payload.append("$%d\r\n" % len(raw))
            payload.append(raw.decode("latin-1") + "\r\n")
        self._sock.sendall("".join(payload).encode("latin-1"))

    def _read(self):
        line = self._file.readline()
        if not line:
            raise ConnectionError("valkey закрыл соединение")
        kind, body = line[:1], line[1:].strip()
        if kind == b"+":
            return body.decode()
        if kind == b":":
            return int(body)
        if kind == b"-":
            raise RuntimeError("valkey: " + body.decode())
        if kind == b"$":
            length = int(body)
            if length == -1:
                return None
            data = self._file.read(length + 2)[:-2]
            return data.decode()
        if kind == b"*":
            return [self._read() for _ in range(int(body))]
        raise RuntimeError("valkey: неизвестный ответ %r" % line)

    def cmd(self, *args):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._send(*args)
                    return self._read()
                except (OSError, ConnectionError):
                    # Одна попытка переподключения: контейнер valkey мог быть перезапущен.
                    self._sock = None
                    if attempt == 2:
                        raise
            return None


VALKEY = Valkey(VALKEY_HOST, VALKEY_PORT)


def _period_keys(engine):
    today = datetime.date.today()
    return ("budget:%s:%s" % (engine, today.strftime("%Y-%m")),
            "budget:%s:%s" % (engine, today.isoformat()))


def budget_state(engine):
    month_key, day_key = _period_keys(engine)
    try:
        used = VALKEY.cmd("MGET", month_key, day_key)
    except (OSError, RuntimeError) as exc:
        return {"engine": engine, "error": str(exc)}
    month_used = int(used[0] or 0)
    day_used = int(used[1] or 0)
    limits = LIMITS.get(engine, {"month": 0, "day": 0})
    return {
        "engine": engine,
        "month_used": month_used,
        "month_limit": limits["month"],
        "month_left": limits["month"] - month_used,
        "day_used": day_used,
        "day_limit": limits["day"],
        "day_left": limits["day"] - day_used,
    }


def budget_allows(state):
    return "error" not in state and state["month_left"] > 0 and state["day_left"] > 0


def budget_spend(engine):
    month_key, day_key = _period_keys(engine)
    # Срок жизни с запасом: месячный ключ переживает конец месяца, дневной — сутки.
    VALKEY.cmd("INCR", month_key)
    VALKEY.cmd("EXPIRE", month_key, 60 * 60 * 24 * 40)
    VALKEY.cmd("INCR", day_key)
    VALKEY.cmd("EXPIRE", day_key, 60 * 60 * 24 * 2)


def upstream(query, engines, language):
    params = {"q": query, "format": "json", "safesearch": 0}
    if engines:
        params["engines"] = ",".join(engines)
    if language:
        params["language"] = language
    url = SEARXNG_URL + "/search?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=UPSTREAM_TIMEOUT) as response:
        return json.load(response)


def extract_answers(payload):
    answers = []
    for item in payload.get("answers", []):
        current = item.get("current") or {}
        text = current.get("summary") or item.get("answer") or item.get("summary")
        if text:
            answers.append(text)
    answers += [box["content"] for box in payload.get("infoboxes", []) if box.get("content")]
    return answers


def accept(payload, pack):
    """Отбирает пригодные результаты. Пусто -> ярус не справился.

    Для пакета оставляем только его домены: движок может вернуть непустую выдачу
    не по теме, и такой ответ хуже пустого — агент примет мусор за находку.
    """
    results = []
    domains = PACKS.get(pack) if pack else None
    for item in payload.get("results", []):
        url = item.get("url") or ""
        if domains and not any(domain in url for domain in domains):
            continue
        results.append({
            "url": url,
            "title": item.get("title") or "",
            "content": item.get("content") or "",
            "engine": item.get("engine") or "",
        })
    return results


def search(query, pack, limit, language):
    warnings = []
    free_engines = [pack] if pack else FREE_ENGINES

    try:
        payload = upstream(query, free_engines, language)
    except (OSError, ValueError) as exc:
        payload = {"results": []}
        warnings.append("ярус A недоступен: %s" % exc)

    results = accept(payload, pack)
    answers = extract_answers(payload)
    dead = [name for name, _ in payload.get("unresponsive_engines", [])]
    if dead:
        warnings.append("не ответили на ярусе A: " + ", ".join(dead))

    if results or answers:
        return {
            "query": query, "pack": pack, "tier": "free", "escalated": False,
            "engines_used": free_engines, "answers": answers,
            "results": results[:limit], "warnings": warnings,
        }

    # Ярус A ничего пригодного не дал — идём на платный, начиная с самого свободного ключа.
    ranked = sorted((budget_state(e) for e in PAID_ENGINES),
                    key=lambda s: s.get("month_left", -1), reverse=True)
    for state in ranked:
        engine = state["engine"]
        if not budget_allows(state):
            warnings.append("%s пропущен: бюджет исчерпан" % engine)
            continue

        paid_query = query
        domains = PACKS.get(pack) if pack else None
        if domains:
            paid_query = "%s %s" % (query, " OR ".join("site:" + d for d in domains))

        try:
            paid_payload = upstream(paid_query, [engine], language)
        except (OSError, ValueError) as exc:
            warnings.append("%s недоступен: %s" % (engine, exc))
            continue

        # Списываем, только если движок реально сходил к поставщику. SearXNG отвечает
        # 200 и когда движок отвалился (невалидный ключ -> 401 -> unresponsive_engines):
        # засчитать такой запрос значило бы жечь квоту за несостоявшийся вызов.
        failed = {name: reason for name, reason in paid_payload.get("unresponsive_engines", [])}
        if engine in failed:
            warnings.append("%s не ответил: %s" % (engine, failed[engine]))
            continue

        budget_spend(engine)
        paid_results = accept(paid_payload, pack)
        paid_answers = extract_answers(paid_payload)
        if paid_results or paid_answers:
            return {
                "query": query, "pack": pack, "tier": "paid", "escalated": True,
                "engines_used": [engine], "answers": paid_answers,
                "results": paid_results[:limit], "warnings": warnings,
            }
        warnings.append("%s не дал результатов" % engine)

    return {
        "query": query, "pack": pack, "tier": "none", "escalated": True,
        "engines_used": [], "answers": [], "results": [], "warnings": warnings,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "search-router"

    def _reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - имя задано BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            self._reply(200, {"status": "ok", "packs": sorted(PACKS), "paid": PAID_ENGINES})
            return

        if parsed.path == "/budget":
            self._reply(200, {"budget": [budget_state(e) for e in PAID_ENGINES]})
            return

        if parsed.path != "/search":
            self._reply(404, {"error": "неизвестный путь", "known": ["/search", "/budget", "/health"]})
            return

        terms = (query.get("q") or [""])[0].strip()
        if not terms:
            self._reply(400, {"error": "нужен параметр q"})
            return

        pack = (query.get("pack") or [None])[0]
        if pack and pack not in PACKS:
            self._reply(400, {"error": "неизвестный пакет: %s" % pack, "known": sorted(PACKS)})
            return

        try:
            limit = max(1, min(50, int((query.get("limit") or ["5"])[0])))
        except ValueError:
            limit = 5
        language = (query.get("language") or [None])[0]

        try:
            self._reply(200, search(terms, pack, limit, language))
        except Exception as exc:  # отдаём причину, а не пустую выдачу: иначе она неотличима от «не нашлось»
            self._reply(502, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    print("search-router: upstream=%s, бесплатный ярус=%s, платный=%s, пакетов=%d"
          % (SEARXNG_URL, FREE_ENGINES, PAID_ENGINES, len(PACKS)), flush=True)
    ThreadingHTTPServer(("0.0.0.0", ROUTER_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
