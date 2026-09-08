#!/usr/bin/env python3
"""Показывает расход квоты Tavily: лимит плана и число запросов через SearXNG.

Два источника, ни один не полон сам по себе:

* GET /usage у Tavily знает план и лимит (1000/мес), но расход отражает с
  задержкой — сразу после запроса plan_usage остаётся прежним, так что для
  оперативного контроля он не годится;
* счётчик SearXNG (searxng_engines_request_count_total) реагирует мгновенно,
  но обнуляется при перезапуске контейнера и не видит запросов мимо инстанса.

Поэтому печатаются оба: лимит — из Tavily, текущий расход — из метрик.

Ключи ищутся как TAVILY_API_KEY и TAVILY_API_KEY_<любой суффикс>.
"""
import base64
import json
import pathlib
import re
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
USAGE_URL = "https://api.tavily.com/usage"
METRICS_URL = "http://127.0.0.1:8080/metrics"


def read_keys(path):
    keys = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip("'\"")
        if name.startswith("TAVILY_API_KEY") and value:
            keys[name] = value
    return keys


def fetch_usage(key):
    request = urllib.request.Request(USAGE_URL, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def read_local_counts(env):
    """Число запросов к движкам tavily* по метрикам SearXNG."""
    password = env.get("METRICS_PASSWORD")
    if not password:
        return {}
    auth = base64.b64encode(("searxng:" + password).encode()).decode()
    request = urllib.request.Request(METRICS_URL, headers={"Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
    except OSError:
        return {}

    counts = {}
    for line in body.splitlines():
        found = re.match(r'searxng_engines_request_count_total\{engine_name="(tavily[^"]*)"\} ([\d.]+)', line)
        if found:
            counts[found.group(1)] = int(float(found.group(2)))
    return counts


def main():
    keys = read_keys(ENV)
    if not keys:
        raise SystemExit("в .env нет ни одного TAVILY_API_KEY*")

    env = {
        k.strip(): v.strip().strip("'\"")
        for k, _, v in (l.partition("=") for l in ENV.read_text().splitlines())
        if k.strip() and not k.strip().startswith("#") and v
    }
    local = read_local_counts(env)

    print("%-24s %-12s %8s %8s %10s" % ("переменная", "ключ", "лимит", "план", "через SearXNG"))
    for name, key in sorted(keys.items()):
        try:
            data = fetch_usage(key)
        except urllib.error.HTTPError as exc:
            print("%-24s %-10s  ОШИБКА HTTP %s" % (name, key[:9] + "…", exc.code))
            continue
        except OSError as exc:
            print("%-24s %-10s  НЕДОСТУПЕН: %s" % (name, key[:9] + "…", exc))
            continue

        account = data.get("account") or {}
        limit = account.get("plan_limit")
        used = account.get("plan_usage")
        remaining = limit - used if isinstance(limit, int) and isinstance(used, int) else "?"
        counted = sum(local.values()) if name == "TAVILY_API_KEY" else local.get(name.lower(), 0)
        print("%-24s %-12s %8s %8s %10s   осталось по плану %s"
              % (name, key[:9] + "…", limit, used, counted, remaining))
    if not local:
        print("\n(метрики недоступны: не задан METRICS_PASSWORD в .env или инстанс не запущен)")


if __name__ == "__main__":
    main()
