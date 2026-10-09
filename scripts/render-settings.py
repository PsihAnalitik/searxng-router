#!/usr/bin/env python3
"""Prepare SearXNG settings and router packs in a dedicated runtime volume."""
import json
import os
import pathlib
import re
import secrets

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE_NAME = re.compile(r"^\s*-\s*name:\s*(.+?)\s*$")
SEARCH_URL = re.compile(r"^\s*search_url:\s*(\S+)\s*$")
SITE_FILTER = re.compile(r"site(?::|%3A)([A-Za-z0-9.\-]+)")


def extract_packs(text):
    """Keep domain packs derived from the existing SearXNG template."""
    packs = {}
    current = None
    for line in text.splitlines():
        found_name = ENGINE_NAME.match(line)
        if found_name:
            current = found_name.group(1)
        found_url = SEARCH_URL.match(line)
        if found_url and current:
            domains = SITE_FILTER.findall(found_url.group(1))
            if domains:
                packs[current] = sorted(set(domains))
    return packs


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".new")
    staging.write_text(text, encoding="utf-8")
    staging.chmod(0o644)
    staging.replace(path)


def render(output, env):
    output.mkdir(parents=True, exist_ok=True)
    secret_file = output / "secret"
    secret = env.get("SEARXNG_SECRET", "").strip()
    if not secret:
        secret = secret_file.read_text().strip() if secret_file.exists() else secrets.token_hex(32)
    write_atomic(secret_file, secret + "\n")
    secret_file.chmod(0o600)
    cx = env.get("GOOGLE_CSE_CX", "").strip()
    cse = ""
    if cx:
        cse = ("  - name: mycse\n    engine: google_cse\n    shortcut: my\n"
               "    categories: [trusted]\n    timeout: 15.0\n    CX: " + json.dumps(cx))
    substitutions = {
        "__SEARXNG_SECRET__": json.dumps(secret),
        "__METRICS_PASSWORD__": json.dumps(env.get("METRICS_PASSWORD", "")),
        "__OPTIONAL_CSE__": cse,
        "__PACK_ENGINES__": "google,mycse" if cx else "google,yandex",
    }
    for name, source in (("core", "core-config/settings.template.yml"),
                         ("backend", "backend-config/settings.template.yml")):
        text = (ROOT / source).read_text(encoding="utf-8")
        text = re.sub(r"__[A-Z0-9_]+__", lambda match: substitutions[match.group()], text)
        write_atomic(output / name / "settings.yml", text)
        if name == "core":
            write_atomic(output / "packs.json", json.dumps(extract_packs(text), indent=2, sort_keys=True) + "\n")


def main():
    render(pathlib.Path(os.environ.get("CONFIG_OUTPUT", "/config")), os.environ)
    print("SearXNG configuration prepared (no paid credentials in SearXNG settings)")


if __name__ == "__main__":
    main()
