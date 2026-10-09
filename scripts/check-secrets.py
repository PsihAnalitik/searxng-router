#!/usr/bin/env python3
"""Check tracked Git history and current text for common credentials, never print values."""
import pathlib
import re
import subprocess

PATTERNS = [re.compile(r'tvly-[A-Za-z0-9_-]{20,}'),
            re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),
            re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
            re.compile(r'(?m)^(?:(?:TAVILY|YANDEX)_API_KEY\w*|METRICS_PASSWORD|SEARXNG_SECRET)=[^\s#<][^\n]{7,}$')]


def main():
    hits = []
    commits = subprocess.check_output(['git', 'rev-list', '--all'], text=True).splitlines()
    for commit in commits:
        files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', commit], text=True).splitlines()
        for file in files:
            text = subprocess.check_output(['git', 'show', commit + ':' + file]).decode('utf-8', 'replace')
            if any(pattern.search(text) for pattern in PATTERNS):
                hits.append(commit[:8] + ':' + file)
    files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard'], text=True).splitlines()
    for file in files:
        path = pathlib.Path(file)
        if path.is_file() and any(pattern.search(path.read_text(errors='replace')) for pattern in PATTERNS):
            hits.append('working-tree:' + file)
    if hits:
        raise SystemExit('Possible credentials in: ' + ', '.join(hits))
    print('PASS: common credential patterns absent from Git history and publishable files')


if __name__ == '__main__':
    main()
