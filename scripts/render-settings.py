#!/usr/bin/env python3
"""Собирает core-config/settings.yml из шаблона, подставляя ключи из .env.

Нужен потому, что SearXNG не умеет читать значения настроек из переменных
окружения (environ_name работает только для secret_key и подобных), а держать
API-ключи в редактируемом конфиге нельзя. Правим шаблон, а не settings.yml.

Заодно пишет router/packs.json — список доменов каждого тематического пакета,
вынутый из его search_url. Так у роутера и у движков один источник истины:
иначе фильтр по доменам разъедется с самими пакетами и начнёт молча резать
верную выдачу.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "core-config" / "settings.template.yml"
TARGET = ROOT / "core-config" / "settings.yml"
PACKS = ROOT / "router" / "packs.json"
ENV = ROOT / ".env"

PLACEHOLDER = re.compile(r"__([A-Z0-9_]+)__")
ENGINE_NAME = re.compile(r"^\s*-\s*name:\s*(.+?)\s*$")
SEARCH_URL = re.compile(r"^\s*search_url:\s*(\S+)\s*$")
SITE_FILTER = re.compile(r"site(?::|%3A)([A-Za-z0-9.\-]+)")


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def extract_packs(text):
    """Домены пакетов из search_url: имя движка -> список доменов."""
    packs = {}
    current = None
    for line in text.splitlines():
        found_name = ENGINE_NAME.match(line)
        if found_name:
            current = found_name.group(1)
            continue
        found_url = SEARCH_URL.match(line)
        if found_url and current:
            domains = SITE_FILTER.findall(found_url.group(1))
            if domains:
                packs[current] = sorted(set(domains))
    return packs


def main():
    env = read_env(ENV)
    text = TEMPLATE.read_text()

    missing = sorted({m.group(1) for m in PLACEHOLDER.finditer(text) if not env.get(m.group(1))})
    if missing:
        # Молча подставить пустой ключ нельзя: движок будет отвечать 401 без внятной причины.
        sys.exit("не заданы в .env: " + ", ".join(missing))

    rendered = PLACEHOLDER.sub(lambda m: env[m.group(1)], text)
    TARGET.write_text(rendered)
    print("собран %s (подставлено плейсхолдеров: %d)"
          % (TARGET.relative_to(ROOT), len(PLACEHOLDER.findall(text))))

    packs = extract_packs(text)
    PACKS.parent.mkdir(exist_ok=True)
    PACKS.write_text(json.dumps(packs, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print("собран %s (пакетов: %d)" % (PACKS.relative_to(ROOT), len(packs)))


if __name__ == "__main__":
    main()
