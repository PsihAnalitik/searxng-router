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

import base64
import datetime
import json
import http.client
import logging
import pathlib
import re
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://core:8080")
VALKEY_HOST = os.environ.get("VALKEY_HOST", "valkey")
VALKEY_PORT = int(os.environ.get("VALKEY_PORT", "6379"))
ROUTER_PORT = int(os.environ.get("ROUTER_PORT", "8090"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "45"))

FREE_ENGINES = [e.strip() for e in os.environ.get(
    "FREE_ENGINES", "google,yandex,bing,duckduckgo web").split(",") if e.strip()]
TAVILY_URL = "https://api.tavily.com/search"
YANDEX_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
PROVIDERS = {"tavily": "TAVILY", "yandex-api": "YANDEX"}
PACKS_FILE = os.environ.get("PACKS_FILE", str(pathlib.Path(__file__).with_name("packs.json")))


def configured_domains(value):
    """Parse bare DNS names; a typo must not silently disable filtering."""
    if not value.strip():
        return []
    domains = []
    for entry in value.split(","):
        domain = entry.strip().rstrip(".").lower().encode("idna").decode("ascii")
        if len(domain) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in domain.split(".")):
            raise ValueError("ALLOWED_DOMAINS must contain comma-separated bare domain names")
        domains.append(domain)
    return domains


ALLOWED_DOMAINS = configured_domains(os.environ.get("ALLOWED_DOMAINS", ""))


def provider_name(engine):
    for name in PROVIDERS:
        if engine == name or engine.startswith(name + "-"):
            return name
    raise ValueError("Unsupported paid engine: " + engine)


def provider_option(env, engine, field):
    provider = provider_name(engine)
    suffix = engine[len(provider):].upper().replace("-", "_")
    prefix = PROVIDERS[provider] + "_" + field
    return env.get(prefix + suffix, "").strip() or env.get(prefix, "").strip()


def configured_keys(env):
    """Discover selected credentials; a provider without a key is never enabled."""
    available = {}
    for provider, prefix in PROVIDERS.items():
        for name, value in sorted(env.items()):
            match = re.fullmatch(prefix + r"_API_KEY(?:_([A-Z0-9_]+))?", name)
            if match and value.strip():
                engine = provider + ("-" + match[1].lower().replace("_", "-") if match[1] else "")
                available[engine] = value.strip()
    selected = env.get("PAID_ENGINES", "auto").strip()
    if selected != "auto":
        names = [name.strip() for name in selected.split(",") if name.strip()]
        for name in names:
            provider_name(name)
        available = {name: available[name] for name in names if name in available}
    seen = set()
    for engine, key in available.items():
        provider = provider_name(engine)
        if (provider, key) in seen:
            raise ValueError("Duplicate provider keys would create independent budgets for one credential")
        seen.add((provider, key))
        if provider == "yandex-api" and not provider_option(env, engine, "FOLDER_ID"):
            raise ValueError(engine + " requires YANDEX_FOLDER_ID (or its key-specific suffix)")
    return available


PAID_KEYS = configured_keys(os.environ)
PAID_ENGINES = list(PAID_KEYS)
PAID_OPTIONS = {engine: {"folder_id": provider_option(os.environ, engine, "FOLDER_ID")}
                for engine in PAID_ENGINES if provider_name(engine) == "yandex-api"}


def _limits():
    out = {}
    for engine in PAID_ENGINES:
        key = engine.upper().replace("-", "_")
        limits = {period: int(os.environ.get("LIMIT_%s_%s" % (key, period.upper()), default))
                  for period, default in (("month", "1000"), ("day", "50"))}
        if min(limits.values()) < 0:
            raise ValueError("Budget limits must be non-negative")
        out[engine] = limits
    return out


LIMITS = _limits()


def load_packs():
    # Missing/corrupt packs are configuration errors, never disable filtering silently.
    with open(PACKS_FILE, encoding="utf-8") as handle:
        return json.load(handle)


PACKS = load_packs()


class Valkey:
    """Минимальный клиент RESP: MGET для состояния, EVAL для атомарного резерва.

    Один сериализованный RESP-сеанс; команды с неопределённым исходом не повторяются.
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

    def close(self):
        if self._file is not None:
            self._file.close()
        if self._sock is not None:
            self._sock.close()
        self._file = self._sock = None

    def cmd(self, *args):
        with self._lock:
            try:
                if self._sock is None:
                    self._connect()
                self._send(*args)
                return self._read()
            except (OSError, RuntimeError, ValueError):
                # A lost reply may follow a committed EVAL. Never replay a mutation.
                self.close()
                raise


VALKEY = Valkey(VALKEY_HOST, VALKEY_PORT)


def _period_keys(engine):
    today = datetime.date.today()
    return ("budget:%s:%s" % (engine, today.strftime("%Y-%m")),
            "budget:%s:%s" % (engine, today.isoformat()))


def budget_state(engine):
    month_key, day_key = _period_keys(engine)
    try:
        used = VALKEY.cmd("MGET", month_key, day_key)
    except (OSError, RuntimeError, ValueError) as exc:
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


RESERVE_SCRIPT = """
local month = tonumber(redis.call('GET', KEYS[1]) or '0')
local day = tonumber(redis.call('GET', KEYS[2]) or '0')
if month >= tonumber(ARGV[1]) or day >= tonumber(ARGV[2]) then return 0 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], 3456000)
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], 172800)
return 1
"""


def budget_reserve(engine):
    """Count attempts before dispatch, including failures with unknown provider outcome."""
    return bool(VALKEY.cmd("EVAL", RESERVE_SCRIPT, 2, *_period_keys(engine),
                           LIMITS[engine]["month"], LIMITS[engine]["day"]))


def tavily_search(query, engine, pack):
    body = {"query": query, "max_results": 10, "search_depth": "basic",
            "auto_parameters": False, "include_answer": False}
    if pack or ALLOWED_DOMAINS:
        body["include_domains"] = PACKS[pack] if pack else ALLOWED_DOMAINS
    request = urllib.request.Request(
        TAVILY_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + PAID_KEYS[engine]},
        method="POST")
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("Invalid Tavily response")
    for item in payload["results"]:
        if isinstance(item, dict):
            item["engine"] = engine
    return payload


class ProviderResponseError(ValueError):
    """Sanitized provider status suitable for a client warning."""


def yandex_search(query, engine, pack, language):
    if pack:
        query = "(%s) (%s)" % (query, " | ".join("site:" + domain for domain in PACKS[pack]))
    if len(query) > 400:
        raise ValueError("Yandex query exceeds 400 characters")
    body = {"query": {"searchType": "SEARCH_TYPE_COM" if language and language.startswith("en") else "SEARCH_TYPE_RU",
                      "queryText": query, "familyMode": "FAMILY_MODE_NONE"},
            "folderId": PAID_OPTIONS[engine]["folder_id"], "responseFormat": "FORMAT_XML",
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "10", "docsInGroup": "1"}}
    request = urllib.request.Request(
        YANDEX_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Api-Key " + PAID_KEYS[engine]},
        method="POST")
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("rawData"), str):
        raise ValueError("Invalid Yandex response")
    xml = base64.b64decode(payload["rawData"], validate=True).decode("utf-8")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("Unexpected XML declarations")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("Invalid Yandex XML") from exc
    error = root.find(".//error")
    if error is not None:
        if error.get("code") == "15":
            return {"results": []}
        code = error.get("code", "")
        safe_code = code if code.isascii() and code.isdigit() and len(code) <= 5 else "unknown"
        raise ProviderResponseError("Yandex XML error " + safe_code)
    if root.tag != "yandexsearch" or root.find("response") is None:
        raise ValueError("Invalid Yandex XML response")
    results = []
    for doc in root.findall(".//doc"):
        title = doc.find("title")
        results.append({"url": doc.findtext("url", ""),
                        "title": "".join(title.itertext()) if title is not None else "",
                        "content": " ".join("".join(p.itertext()) for p in doc.findall(".//passage")),
                        "engine": engine})
    return {"results": results}


def paid_search(query, engine, pack, language):
    provider = provider_name(engine)
    if provider == "tavily":
        return tavily_search(query, engine, pack)
    return yandex_search(query, engine, pack, language)


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
    items = payload.get("answers", [])
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            answers.append(item)
        elif isinstance(item, dict):
            current = item.get("current")
            current = current if isinstance(current, dict) else {}
            text = current.get("summary") or item.get("answer") or item.get("summary")
            if isinstance(text, str):
                answers.append(text)
    boxes = payload.get("infoboxes", [])
    for box in boxes if isinstance(boxes, list) else []:
        if isinstance(box, dict) and isinstance(box.get("content"), str):
            answers.append(box["content"])
    return answers


def accept(payload, pack):
    """Keep HTTP(S) links passing both the optional global and pack allowlists."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("search response must contain a results list")
    results = []
    domain_lists = [domains for domains in (ALLOWED_DOMAINS, PACKS.get(pack)) if domains]
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".").encode("idna").decode("ascii")
            parsed.port  # Validate malformed/non-numeric ports as well.
        except (ValueError, UnicodeError):
            continue
        if parsed.scheme not in ("http", "https") or not host or parsed.username is not None:
            continue
        if not all(any(host == domain or host.endswith("." + domain) for domain in domains)
                   for domains in domain_lists):
            continue
        results.append({
            "url": url,
            "title": item.get("title") if isinstance(item.get("title"), str) else "",
            "content": item.get("content") if isinstance(item.get("content"), str) else "",
            "engine": item.get("engine") if isinstance(item.get("engine"), str) else "",
        })
    return results


def search(query, pack, limit, language):
    warnings = []
    attempted_paid = False
    free_engines = [pack] if pack else FREE_ENGINES

    try:
        payload = upstream(query, free_engines, language)
        results = accept(payload, pack)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        payload, results = {}, []
        warnings.append("ярус A: нет корректного ответа (%s)" % type(exc).__name__)

    dead = payload.get("unresponsive_engines", [])
    for item in dead if isinstance(dead, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(v, str) for v in item):
            warnings.append("ярус A: %s: %s" % tuple(item))

    if results:
        return {
            "query": query, "pack": pack, "tier": "free", "escalated": False,
            "engines_used": free_engines,
            "answers": [] if pack or ALLOWED_DOMAINS else extract_answers(payload),
            "results": results[:limit], "warnings": warnings,
        }
    if payload.get("results"):
        warnings.append("ярус A: нет пригодных ссылок после фильтрации")

    # Ярус A ничего пригодного не дал — идём на платный, начиная с самого свободного ключа.
    ranked = sorted((budget_state(e) for e in PAID_ENGINES),
                    key=lambda s: s.get("month_left", -1), reverse=True)
    for state in ranked:
        engine = state["engine"]
        if "error" in state:
            warnings.append("%s пропущен: хранилище бюджета недоступно" % engine)
            continue
        if not budget_allows(state):
            warnings.append("%s пропущен: бюджет исчерпан" % engine)
            continue

        try:
            reserved = budget_reserve(engine)
        except (OSError, RuntimeError, ValueError):
            warnings.append("%s пропущен: не удалось зарезервировать бюджет" % engine)
            continue
        if not reserved:
            warnings.append("%s пропущен: бюджет исчерпан" % engine)
            continue
        attempted_paid = True
        try:
            paid_payload = paid_search(query, engine, pack, language)
            paid_results = accept(paid_payload, pack)
        except urllib.error.HTTPError as exc:
            # Do not expose response bodies or credentials in errors.
            warnings.append("%s: HTTP %s; попытка учтена в бюджете" % (engine, exc.code))
            exc.close()
            continue
        except ProviderResponseError as exc:
            warnings.append("%s: %s; попытка учтена в бюджете" % (engine, exc))
            continue
        except (OSError, ValueError, http.client.HTTPException):
            warnings.append("%s: нет корректного ответа; попытка учтена в бюджете" % engine)
            continue

        paid_answers = [] if pack or ALLOWED_DOMAINS else extract_answers(paid_payload)
        if paid_results:
            return {
                "query": query, "pack": pack, "tier": "paid", "escalated": True,
                "engines_used": [engine], "answers": paid_answers,
                "results": paid_results[:limit], "warnings": warnings,
            }
        warnings.append("%s не дал результатов" % engine)

    if not PAID_ENGINES:
        warnings.append("бесплатный поиск не дал результатов; платный резерв отключён")
    return {
        "query": query, "pack": pack, "tier": "none", "escalated": attempted_paid,
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
        except Exception:
            logging.exception("Search failed")
            self._reply(502, {"error": "внутренняя ошибка поиска"})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    print("search-router: upstream=%s, бесплатный ярус=%s, платный=%s, пакетов=%d"
          % (SEARXNG_URL, FREE_ENGINES, PAID_ENGINES, len(PACKS)), flush=True)
    ThreadingHTTPServer(("0.0.0.0", ROUTER_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
