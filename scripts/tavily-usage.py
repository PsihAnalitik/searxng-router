#!/usr/bin/env python3
"""Show provider usage and router attempt budgets. Run explicitly; calls Tavily /usage."""
import json
import pathlib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
USAGE_URL = "https://api.tavily.com/usage"
ROUTER_URL = "http://127.0.0.1:{port}/budget"


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
    """Return per-engine router reservations, not SearXNG request metrics."""
    url = ROUTER_URL.format(port=int(env.get("ROUTER_PORT", "8090")))
    with urllib.request.urlopen(url, timeout=10) as response:
        return {state["engine"]: state for state in json.load(response)["budget"]}


def main():
    keys = read_keys(ENV)
    if not keys:
        print("Платный резерв не настроен: в .env нет ключей Tavily")
        return

    env = {
        k.strip(): v.strip().strip("'\"")
        for k, _, v in (l.partition("=") for l in ENV.read_text().splitlines())
        if k.strip() and not k.strip().startswith("#") and v
    }
    try:
        local = read_local_counts(env)
    except (OSError, ValueError):
        local = {}
        print("Локальный бюджет недоступен; проверьте router и ROUTER_PORT")

    print("%-24s %-12s %8s %8s %10s" % ("переменная", "ключ", "лимит", "план", "попытки/мес"))
    for name, key in sorted(keys.items()):
        try:
            data = fetch_usage(key)
        except urllib.error.HTTPError as exc:
            print("%-24s %-10s  ОШИБКА HTTP %s" % (name, "скрыт", exc.code))
            continue
        except OSError as exc:
            print("%-24s %-10s  НЕДОСТУПЕН: %s" % (name, "скрыт", exc))
            continue

        account = data.get("account") or {}
        limit = account.get("plan_limit")
        used = account.get("plan_usage")
        remaining = limit - used if isinstance(limit, int) and isinstance(used, int) else "?"
        suffix = name.removeprefix("TAVILY_API_KEY").lower().replace("_", "-")
        counted = local.get("tavily" + suffix, {}).get("month_used", "—")
        print("%-24s %-12s %8s %8s %10s   осталось по плану %s"
              % (name, "скрыт", limit, used, counted, remaining))


if __name__ == "__main__":
    main()
