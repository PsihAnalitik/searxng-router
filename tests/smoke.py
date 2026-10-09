#!/usr/bin/env python3
"""Clean-checkout Docker smoke test. No external searches or real API keys."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ['docker-compose.yml', '.dockerignore', 'router/Dockerfile', 'router/app.py',
         'router/packs.json', 'scripts/render-settings.py',
         'core-config/settings.template.yml', 'backend-config/settings.template.yml']


def main():
    work = ROOT / '.test-work'
    work.mkdir(exist_ok=True)
    project = 'searxng-smoke-' + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(dir=work) as directory:
        dest = pathlib.Path(directory)
        for file in FILES:
            target = dest / file
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / file, target)
        shutil.copyfile(ROOT / '.env.example', dest / '.env')
        # Host credentials/config overrides must not influence the clean-clone fixture.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('SEARXNG_', 'ROUTER_', 'TAVILY_', 'YANDEX_', 'LIMIT_', 'COMPOSE_'))
               and key not in ('PAID_ENGINES', 'FREE_ENGINES', 'GOOGLE_CSE_CX', 'METRICS_PASSWORD')}
        def run(args, **kwargs):
            return subprocess.run(args, check=True, env=env, text=True, **kwargs)
        cfg = json.loads(run(['docker', 'compose', '--project-directory', str(dest),
                              '--env-file', str(dest / '.env'), 'config', '--format', 'json'],
                             capture_output=True).stdout)
        cfg['name'] = project
        cfg['networks'] = {'default': {'name': project + '_default', 'internal': True}}
        for service in cfg['services'].values():
            service.pop('ports', None)
            service['restart'] = 'no'
        for name, volume in cfg['volumes'].items():
            volume['name'] = project + '_' + name
        config = dest / 'compose.json'
        def save():
            config.write_text(json.dumps(cfg, indent=2))
        save()
        compose = ['docker', 'compose', '-f', str(config), '-p', project]
        def python(code):
            return run(compose + ['exec', '-T', 'router', 'python', '-'], input=code,
                       capture_output=True).stdout.strip()
        try:
            run(compose + ['up', '-d', '--build', '--wait', '--wait-timeout', '180'])
            print(python("""
import json, urllib.request
from pathlib import Path
health = json.load(urllib.request.urlopen('http://localhost:8090/health'))
assert health['paid'] == [], health
assert len(health['packs']) == 11, health
for url in ['http://core:8080/config', 'http://backend:8080/config',
            'http://searxng-core:8080/config', 'http://searxng-backend:8080/config']:
    config = json.load(urllib.request.urlopen(url))
    assert not any(e['name'].startswith('tavily') for e in config['engines']), url
assert json.load(urllib.request.urlopen('http://localhost:8090/budget')) == {'budget': []}
print('PASS: clean-clone free-only HTTP/config, 11 packs, no Tavily engines')
"""))
            # Source stays read-only; generated test artifacts stay inside this fixture.
            (dest / 'unit-work').mkdir()
            run(['docker', 'run', '--rm', '--network', project + '_default',
                 '-e', 'TEST_VALKEY_HOST=valkey', '-v', str(ROOT) + ':/src:ro',
                 '-v', str(dest / 'unit-work') + ':/src/.test-work', '-w', '/src',
                 project + '-router', 'python', '-B', '-m', 'unittest', 'discover',
                 '-s', 'tests', '-v'])
            run(compose + ['exec', '-T', 'valkey', 'valkey-cli', 'SET', 'budget:smoke:persist', '7'], capture_output=True)
            run(compose + ['restart', 'valkey'])
            run(compose + ['up', '-d', '--wait', '--wait-timeout', '60', 'valkey'])
            persisted = run(compose + ['exec', '-T', 'valkey', 'valkey-cli', 'GET', 'budget:smoke:persist'], capture_output=True).stdout.strip()
            assert persisted == '7', persisted
            print('PASS: existing budget values survive Valkey restart')
            router_env = cfg['services']['router']['environment']
            router_env.update(TAVILY_API_KEY='smoke-placeholder', TAVILY_API_KEY_2='smoke-placeholder-2',
                              LIMIT_TAVILY_2_DAY='7', PAID_ENGINES='auto',
                              YANDEX_API_KEY='smoke-yandex', YANDEX_FOLDER_ID='smoke-folder',
                              YANDEX_API_KEY_2='smoke-yandex-2', LIMIT_YANDEX_API_2_DAY='3')
            save()
            run(compose + ['up', '-d', '--no-deps', '--force-recreate', '--wait', 'router'])
            print(python("""
import json, urllib.request
health = json.load(urllib.request.urlopen('http://localhost:8090/health'))
assert health['paid'] == ['tavily', 'tavily-2', 'yandex-api', 'yandex-api-2'], health
states = json.load(urllib.request.urlopen('http://localhost:8090/budget'))['budget']
assert next(s for s in states if s['engine'] == 'tavily-2')['day_limit'] == 7
assert next(s for s in states if s['engine'] == 'yandex-api-2')['day_limit'] == 3
print('PASS: optional keys and second-key limits applied without YAML engine edits')
"""))
            router_env['PAID_ENGINES'] = ''
            save()
            run(compose + ['up', '-d', '--no-deps', '--force-recreate', '--wait', 'router'])
            print(python("""
import json, urllib.request
assert json.load(urllib.request.urlopen('http://localhost:8090/health'))['paid'] == []
print('PASS: explicit empty pool disables configured keys')
"""))
            router_env.update(PAID_ENGINES='auto', TAVILY_API_KEY='', TAVILY_API_KEY_2='',
                              YANDEX_API_KEY='', YANDEX_API_KEY_2='')
            save()
            run(compose + ['up', '-d', '--no-deps', '--force-recreate', '--wait', 'router'])
            print(python("""
import json, urllib.request
assert json.load(urllib.request.urlopen('http://localhost:8090/health'))['paid'] == []
print('PASS: removing keys returns to free-only mode')
"""))
        finally:
            # Only this randomly named fixture and its disposable volumes are removed.
            run(compose + ['down', '-v', '--remove-orphans'])


if __name__ == '__main__':
    main()
