# SearXNG — локальный поисковый бэкенд для агентов

Метапоисковик в docker, поднимается локально и отдаёт результаты в JSON.
Общий сервис для `smart-assistant` и других проектов в `~/projects`.

Основано на официальном шаблоне
[`container/docker-compose.yml`](https://github.com/searxng/searxng/blob/master/container/docker-compose.yml),
документация: <https://docs.searxng.org/admin/installation-docker.html>.

---

## 1. Что здесь лежит

| Файл | Назначение |
|---|---|
| `docker-compose.yml` | три сервиса: `searxng-core` (веб), `searxng-backend` (бэкенд доменных пакетов, наружу не публикуется), `searxng-valkey` (кэш) |
| `backend-config/settings.yml` | конфиг бэкенда: обычный SearXNG без пакетов |
| `core-config/settings.template.yml` | **редактируемый** конфиг с плейсхолдерами ключей |
| `scripts/render-settings.py` | собирает `settings.yml` из шаблона и `.env` |
| `scripts/tavily-usage.py` | расход квоты Tavily |
| `router/` | сервис search-router: каскад ярусов и бюджет ключей |
| `router/packs.json` | домены пакетов, генерируется из шаблона |
| `core-config/settings.yml` | конфиг инстанса; смонтирован в `/etc/searxng/` |
| `.env.example` | шаблон переменных окружения |
| `.env` | реальные значения, **в git не попадает** (`.gitignore`) |

Ключевое отличие от дефолта SearXNG: в `core-config/settings.yml` включён формат `json`
(по умолчанию инстанс отдаёт только HTML) и выключен `limiter` — иначе агент получает 403.

## 2. Запуск

```bash
cd ~/projects/searxng

# один раз: создать .env и сгенерировать секрет
cp .env.example .env
sed -i "s|^SEARXNG_SECRET=.*|SEARXNG_SECRET=$(openssl rand -hex 32)|" .env
chmod 600 .env            # в файле секрет инстанса

docker compose up -d
docker compose ps
```

Инстанс слушает `http://127.0.0.1:8080` (адрес и порт задаются `SEARXNG_HOST` / `SEARXNG_PORT` в `.env`).

Управление:

```bash
docker compose logs -f core   # логи
docker compose restart core   # перечитать settings.yml после правки
docker compose down           # остановить (данные сохраняются)
docker compose down -v        # остановить и удалить кэш-тома
```

## 3. Проверка работоспособности

```bash
# 1. веб-интерфейс
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/

# 2. health-check: конфигурация инстанса в JSON
curl -s http://127.0.0.1:8080/config | head -c 200

# 3. поиск в JSON — то, чем пользуется агент
curl -s 'http://127.0.0.1:8080/search?q=claude+code&format=json' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["results"]), "результатов"); print(d["results"][0]["url"])'
```

Если п.3 вернул `403` — не подхватился `limiter: false`; если пришёл HTML вместо JSON —
в `settings.yml` не включён формат `json`. В обоих случаях: проверить
`core-config/settings.yml` и сделать `docker compose restart core`.

## 4. HTTP API

Один нужный эндпоинт: `GET /search`.

| Параметр | Значение |
|---|---|
| `q` | запрос (обязателен) |
| `format` | `json` |
| `language` | `ru`, `en`, `auto` |
| `categories` | `general`, `news`, `it`, `science`, `images` |
| `engines` | список через запятую, напр. `duckduckgo,brave` |
| `time_range` | `day`, `week`, `month`, `year` |
| `pageno` | номер страницы, с 1 |
| `safesearch` | `0`, `1`, `2` |

Форма ответа:

```json
{
  "query": "...",
  "results": [
    {"url": "https://...", "title": "...", "content": "сниппет", "engine": "google cse", "score": 1.0}
  ],
  "answers": [],
  "corrections": [],
  "infoboxes": [],
  "suggestions": [],
  "unresponsive_engines": [["brave", "too many requests"]]
}
```

Поля `number_of_results` в ответе может не быть вовсе (в проверенной здесь выдаче его
нет), а когда оно есть — нередко равно `0` при непустом `results`. Считать результаты
нужно по `len(results)`, ориентироваться на `number_of_results` нельзя.

Одна страница выдачи — 20 результатов.

`unresponsive_engines` — не ошибка запроса: часть движков может отвалиться по таймауту
или капче, остальные при этом отработают. Пустой `results` при непустом
`unresponsive_engines` означает, что отвалились все — это повод для ретрая, а не для
падения агента.

## 5. Подключение к агенту

### 5.1 Агент запущен на хосте

Базовый URL — `http://127.0.0.1:8080`. В окружение проекта (например
`~/projects/smart-assistant/.env`):

```env
SEARXNG_BASE_URL=http://127.0.0.1:8080
```

### 5.2 Агент запущен в docker

`127.0.0.1` внутри контейнера — это сам контейнер, до SearXNG так не достучаться.
Два рабочих варианта:

**а) подключить контейнер агента к сети SearXNG** (предпочтительно — трафик не выходит на хост):

```bash
docker network connect searxng_default <имя_контейнера_агента>
```

Тогда базовый URL — `http://searxng-core:8080` (обращение по имени сервиса).
Чтобы связь переживала пересоздание контейнера, ту же сеть лучше объявить в compose-файле проекта:

```yaml
services:
  agent:
    networks: [default, searxng]
networks:
  searxng:
    external: true
    name: searxng_default
```

**б) через хост:** базовый URL `http://host.docker.internal:8080`, при этом контейнеру
нужен `extra_hosts: ["host.docker.internal:host-gateway"]`, а в `.env` SearXNG —
`SEARXNG_HOST=` (пусто, слушать все интерфейсы) вместо `127.0.0.1`.

### 5.3 Инструмент агента

Минимальная реализация тула поиска — синхронная, на `httpx`:

```python
import os

import httpx

SEARXNG_BASE_URL = os.environ["SEARXNG_BASE_URL"]

# Пакет -> домены, которые он обязан возвращать. Нужен не для запроса, а для проверки:
# движок может вернуть непустую выдачу не по теме (так делал qwant на кириллице).
PACK_DOMAINS = {
    "dev sites": ("habr.com", "docs.python.org", "wiki.archlinux.org", "developer.mozilla.org"),
    "pogoda": ("gismeteo.ru",),
    "msk transport": ("mos.ru", "mosmetro.ru", "ictransport.ru"),
    "ru politics": ("tass.ru", "ria.ru", "kommersant.ru", "rbc.ru",
                    "duma.gov.ru", "kremlin.ru", "government.ru"),
    "philosophy": ("plato.stanford.edu", "iep.utm.edu", "iphlib.ru"),
    "ru science": ("cyberleninka.ru", "elibrary.ru"),
    "world data": ("numbeo.com", "worldbank.org", "macrotrends.net", "ourworldindata.org"),
    "education": ("openedu.ru", "stepik.org", "mccme.ru", "foxford.ru", "resh.edu.ru"),
    "ru geo": ("rgo.ru", "rosstat.gov.ru", "ru.wikipedia.org"),
    "tadviser": ("tadviser.ru",),
    "reddit": ("reddit.com",),
}


def web_search(query, *, engines=None, limit=5, language="auto"):
    """Поиск через локальный SearXNG.

    engines — имя пакета или движка ("philosophy", "github"); None = общая выдача.
    Возвращает {"answers": [...], "results": [...]}: answers — готовые сводки
    (погода, выжимка из Wikipedia), results — ссылки со сниппетами.
    """
    params = {"q": query, "format": "json", "language": language, "safesearch": 0}
    if engines:
        params["engines"] = engines

    response = httpx.get(f"{SEARXNG_BASE_URL}/search", params=params, timeout=30.0)
    response.raise_for_status()
    payload = response.json()

    # Погода и справки приходят сюда, а не в results — читать только results значит их потерять.
    answers = []
    for a in payload.get("answers", []):
        # У погодных движков сводка лежит во вложенном current, а не на верхнем уровне.
        current = a.get("current") or {}
        text = current.get("summary") or a.get("answer") or a.get("summary")
        if text:
            answers.append(text)
    answers += [b["content"] for b in payload.get("infoboxes", []) if b.get("content")]

    results = []
    allowed = PACK_DOMAINS.get(engines)
    for item in payload["results"]:
        # Пакет обязан возвращать только свои домены; чужое — признак того,
        # что движок проигнорировал site: и подсунул мусор.
        if allowed and not any(d in item["url"] for d in allowed):
            continue
        results.append({
            "url": item["url"],
            "title": item["title"],
            "content": item.get("content", ""),
            "engine": item.get("engine", ""),
        })

    return {"answers": answers, "results": results[:limit]}
```

Замечания по интеграции:

- **Таймаут обязателен.** SearXNG опрашивает внешние движки, и запрос может идти
  секунды; без таймаута агент подвисает.
- **Обрезать выдачу до `limit`.** Инстанс возвращает десятки результатов, полный
  список раздувает контекст модели.
- **Не глотать ошибки.** `raise_for_status()` и проброс исключения наверх; тихий
  `except: return []` превращает недоступный сервис в «ничего не нашлось».
- **Описание тула для модели:** «Ищет актуальную информацию в интернете. Возвращает
  список ссылок со сниппетами. Использовать, когда нужны свежие данные или факты вне
  знаний модели.» Сниппет — не полный текст страницы; за содержимым нужен отдельный
  fetch-тул.

### 5.4 Как подключить к самому Claude Code

Через MCP-сервер, обёртку над этим же HTTP API (например `mcp-searxng`):

```bash
claude mcp add searxng --env SEARXNG_URL=http://127.0.0.1:8080 -- npx -y mcp-searxng
```

После этого `claude mcp list` покажет сервер, а его инструменты станут доступны в сессии.

## 6. Источники и тематические банги

Инстанс настроен не «включить всё», а на проверенный состав: каждый движок ниже
подтверждён реальным запросом, мёртвые выключены, чтобы не тормозить выдачу.

### 6.1 Тематические пакеты

Пакет — это движок, который дописывает к запросу `site:`-фильтр по своим доменам.
Вызывается двумя способами: бангом в тексте запроса или параметром `engines`.

```bash
curl -s 'http://127.0.0.1:8080/search?q=!phil+epistemology&format=json'
curl -s --get 'http://127.0.0.1:8080/search' --data-urlencode 'q=epistemology' \
     -d 'format=json' -d 'engines=philosophy'
```

| Банг | Пакет | Домены | Категория |
|---|---|---|---|
| `!dev` | dev sites | habr.com, docs.python.org, wiki.archlinux.org, developer.mozilla.org | `it` |
| `!pog` | pogoda | gismeteo.ru | `weather` |
| `!msk` | msk transport | mos.ru (со всеми субдоменами), mosmetro.ru, ictransport.ru | `msk` |
| `!rupol` | ru politics | tass.ru, ria.ru, kommersant.ru, rbc.ru, duma.gov.ru, kremlin.ru, government.ru | `news` |
| `!tadv` | tadviser | tadviser.ru | `people` |
| `!rugeo` | ru geo | rgo.ru, rosstat.gov.ru, ru.wikipedia.org | `rugeo` |
| `!world` | world data | numbeo.com, worldbank.org, macrotrends.net, ourworldindata.org | `world` |
| `!rdt` | reddit | reddit.com | `social` |
| `!phil` | philosophy | plato.stanford.edu, iep.utm.edu, iphlib.ru | `philosophy` |
| `!rusci` | ru science | cyberleninka.ru, elibrary.ru | `science` |
| `!edu` | education | openedu.ru, stepik.org, mccme.ru, foxford.ru, resh.edu.ru | `education` |
| `!my` | свой Programmable Search Engine, 20 доверенных сайтов | — | `trusted` |

Категория собирает несколько источников сразу: `categories=it` вернёт и `dev sites`,
и `github`, и `stackoverflow`.

Запрос пакета уходит не в этот же инстанс, а в отдельный `searxng-backend`, и у каждого
пакета в `search_url` **обязательно** задан `language=`. Причина не косметическая:
`json_engine` добавляет к запросу заголовок `Accept-Language: en-US,en;q=0.9`, а приёмная
сторона берёт язык поиска из него. Без явного `language=` кириллические запросы
возвращали 0, а латинские — случайные субдомены (`operatv.gismeteo.ru`) вместо
релевантной выдачи. `language=auto` не спасает: он читает тот же заголовок.
Если добавляете свой пакет — язык задавайте сразу.

### 6.2 Нативные движки по темам

Проверены рабочими и включены:

- **код:** `github` `!gh`, `gitlab` `!gl`, `stackoverflow` `!st`, `askubuntu` `!ubuntu`,
  `superuser` `!su`, `hackernews` `!hn`, `lobste.rs` `!lo`, `mdn` `!mdn`,
  `microsoft learn` `!msl`
- **наука:** `arxiv` `!arx`, `crossref` `!cr`, `openalex` `!oa`, `pubmed` `!pub`
- **новости:** `google news` `!gon`, `bing news` `!bin`, `duckduckgo news` `!ddn`
- **общий веб:** `google` `!go`, `yandex` `!yd`, `bing` `!bi`, `qwant` `!qw`,
  `duckduckgo web` `!ddgw`
- **поиск для агентов:** `tavily` `!tv` — API с ключом, без CAPTCHA и без проблем
  с кириллицей; подробности и учёт квоты в §7
- **погода:** `openmeteo` `!om`, `duckduckgo weather` `!ddw`

Погодные движки кладут данные **не в `results`, а в `answers`** — готовую сводку с
температурой, ощущаемой и прогнозом. `wikipedia` `!wp` так же отдаёт выжимку в
`infoboxes`. Функция агента, читающая только `results`, эти ответы потеряет.

Выключены как стабильно недоступные из этой сети: `brave`, `startpage`, `duckduckgo`
(вариант `!ddgw` при этом работает), `mojeek`, `wikidata`, `arch linux wiki`,
`semantic scholar`, `baidu`, `google scholar`.

### 6.3 Ограничения, которые важно знать агенту

**Пустая выдача не означает, что источник пуст.** Запрос
`нейронные сети site:elibrary.ru` вернул 0, а `машинное обучение site:elibrary.ru` —
9 результатов с того же домена. Правильная реакция на пустой ответ — повторить с другой
формулировкой, а не заключать, что данных нет.

**Совпадения домена мало — проверяйте сам URL.** Пока в пакетах не был задан язык,
они возвращали правильные домены с бессмысленными адресами: `operatv.gismeteo.ru`
вместо `gismeteo.ru/weather-moscow-4368/`. Проверка «домен из списка пакета» такую
поломку пропускает.

**Непустая выдача не означает, что результат релевантен.** Движок `qwant` на
кириллическом запросе с `site:` возвращал спам-домены (`ah.dj`, `wupewwi.az`) вместо
фильтрации — поэтому он и исключён из пакетов. Отсюда правило: **проверяйте, что домен
результата входит в ожидаемый список пакета**, вместо того чтобы доверять длине
`results`.

**Частота.** Простые запросы `google` держит уверенно (15 подряд без отказа), но
запросы с `site:` и `OR` провоцируют антибот — CAPTCHA примерно на пятом подряд.
Свой CSE выдерживает около шести запросов, после чего Google молча отдаёт пустой ответ
без ошибки. Практический режим — единицы запросов на вопрос пользователя, с паузами и
ретраем, а не пакетная обработка. После CAPTCHA движок подвешивается на 300 с
(`suspended_times` в `settings.yml`, снижено с дефолтных 3600).

**Резервирование.** У пакетов два независимых источника: `google` и ваш `mycse`
(оба держат `site:` вместе с `OR`), у одиночных доменов — `google` и `yandex`.
Если оба недоступны одновременно, пакет вернёт 0 **без** ошибки в
`unresponsive_engines` — это и есть сигнал повторить позже.

## 7. Tavily и учёт платных ключей

`tavily` (банг `!tv`) — поисковый API для агентов. В отличие от всего остального в этом
инстансе он не скрейпит выдачу, поэтому у него нет ни CAPTCHA, ни антибота, а
кириллица работает без настройки языка. Это единственный источник, который выдерживает
пакетную нагрузку.

Ключ берётся из `TAVILY_API_KEY` в `.env` и подставляется в конфиг скриптом
(см. §8). Бесплатный план — 1000 запросов в месяц на аккаунт.

### Расход квоты

```bash
python3 scripts/tavily-usage.py
```

```
переменная               ключ            лимит     план через SearXNG
TAVILY_API_KEY           tvly-dev-…       1000        0          2   осталось по плану 1000
```

Источников два, и ни один не полон:

- **`GET https://api.tavily.com/usage`** — знает план и лимит, но расход показывает с
  задержкой: сразу после запроса `plan_usage` остаётся прежним. Для оперативного
  контроля не годится.
- **`/metrics` инстанса** (`searxng_engines_request_count_total{engine_name="tavily"}`) —
  растёт мгновенно, но живёт в памяти: обнуляется при `docker compose restart` и не
  видит запросов, сделанных мимо SearXNG.

Эндпоинт `/metrics` включается параметром `general.open_metrics`; пароль лежит в
`METRICS_PASSWORD` в `.env`, без него SearXNG отдаёт 404:

```bash
curl -s -u "searxng:$(grep ^METRICS_PASSWORD= .env | cut -d= -f2-)" \
     http://127.0.0.1:8080/metrics | grep request_count_total
```

### Несколько ключей

Скрипт читает все переменные вида `TAVILY_API_KEY*`, так что ключи с разных аккаунтов
добавляются в `.env` как `TAVILY_API_KEY_2`, `TAVILY_API_KEY_3` и так далее. Чтобы
расход считался раздельно, каждому ключу нужен **свой движок** в шаблоне —
`tavily-2`, `tavily-3` — со своим плейсхолдером; тогда счётчик
`engine_name="tavily-2"` и будет расходом этого ключа.

Автоматического переключения между ключами SearXNG не делает: движки в одной категории
опрашиваются одновременно, то есть один запрос списал бы квоту со всех сразу. Поэтому
ключ выбирает вызывающая сторона — передавая нужный `engines=tavily-2` — а скрипт выше
показывает, у какого ключа ещё есть запас.

### Ограничение

`json_engine` подставляет запрос в тело POST сырым текстом, поэтому **запрос с двойной
кавычкой ломает JSON** и возвращает HTTP 400. Обходится только собственным модулем
движка; в конфиге место помечено `FIXME`.

## 8. search-router — единая точка входа для агентов

Агенту не нужно знать про ярусы, ключи и бюджеты: он ходит на
`http://127.0.0.1:8090/search`, остальное решает сервис.

### Зачем он есть

SearXNG опрашивает движки категории **строго одновременно** — поток на движок,
понятия fallback в коде нет. Значит правило «сначала бесплатные, если не вышло —
платные» внутри SearXNG невыразимо: один запрос в категорию, где лежит Tavily,
списывал бы квоту всегда. Каскад, бюджет и отбраковку мусора делает роутер.

Что при этом **не** дублируется: параллельный опрос движков и консенсус между ними.
SearXNG взвешивает результат по числу нашедших его движков
(`weight * len(result['positions'])`), поэтому URL, найденный тремя источниками,
уже поднимается выше найденного одним.

### Ярусы

| Ярус | Состав | Когда |
|---|---|---|
| A, бесплатный | `google`, `yandex`, `bing`, `duckduckgo web` — или движок пакета | всегда первым |
| B, платный | `tavily` | только если ярус A не дал пригодного ответа и бюджет позволяет |

Ответ считается пригодным, если в нём есть результаты или готовые ответы, а для
пакета — если после фильтра по его доменам что-то осталось. Пустая выдача и выдача
не с тех доменов (случай `qwant` со спамом) одинаково означают «ярус не справился».

### API

```bash
# общий поиск
curl -s 'http://127.0.0.1:8090/search?q=python+3.13+release+notes&limit=3'

# тематический пакет
curl -s --get 'http://127.0.0.1:8090/search' \
     --data-urlencode 'q=epistemology' -d 'pack=philosophy' -d 'limit=3'

# состояние бюджетов и здоровье сервиса
curl -s http://127.0.0.1:8090/budget
curl -s http://127.0.0.1:8090/health
```

Параметры `/search`: `q` (обязателен), `pack`, `limit` (1–50, по умолчанию 5),
`language`. Ответ:

```json
{
  "query": "...", "pack": "philosophy",
  "tier": "free|paid|none", "escalated": false,
  "engines_used": ["google", "yandex"],
  "answers": ["Moscow: 22 °C, Cloudy"],
  "results": [{"url": "...", "title": "...", "content": "...", "engine": "..."}],
  "warnings": ["не ответили на ярусе A: brave"]
}
```

`tier: "none"` означает, что не справились оба яруса; причина всегда в `warnings` —
это сигнал повторить позже, а не «ничего не нашлось».

### Бюджеты

Счётчики лежат в valkey (`budget:<движок>:<месяц>` и `:<день>`), поэтому переживают
перезапуск сервиса — в отличие от метрик SearXNG, которые живут в памяти. Лимиты
задаются в `.env`: `LIMIT_TAVILY_MONTH` (по умолчанию 1000) и `LIMIT_TAVILY_DAY`
(50). Дневной сублимит нужен, чтобы месячная квота не выгорела за сутки.

При исчерпании лимита платный вызов **не делается**: движок пропускается с
пометкой в `warnings`, счётчик не растёт.

### Несколько ключей Tavily

Ключ зашит в движок, поэтому **на каждый ключ заводится свой движок**. Порядок:

1. В `.env` добавить ключ: `TAVILY_API_KEY_2=tvly-…` и лимиты
   `LIMIT_TAVILY_2_MONTH=1000`, `LIMIT_TAVILY_2_DAY=50`.
2. В `core-config/settings.template.yml` скопировать блок движка `tavily`, поменяв
   `name` на `tavily-2`, `shortcut` на `tv2` и плейсхолдер на `__TAVILY_API_KEY_2__`.
3. Пересобрать конфиг и перечислить движки в `PAID_ENGINES`:
   `PAID_ENGINES=tavily,tavily-2`.

Имя переменной лимита выводится из имени движка: дефисы и пробелы становятся
подчёркиваниями, регистр — верхний. То есть `tavily-2` → `LIMIT_TAVILY_2_MONTH`.

Дальше роутер работает сам:

- **Балансирует.** Перед каждым платным вызовом ключи сортируются по остатку месячной
  квоты, берётся самый свободный, поэтому расход идёт равномерно. Проверено на двух
  ключах: запросы легли `tavily-2`, `tavily`, `tavily-2`, `tavily`.
- **Обходит исчерпанные.** Ключ с выбранным дневным или месячным лимитом
  пропускается с пометкой в `warnings`, запрос уходит на следующий. Проверено:
  при `LIMIT_TAVILY_DAY=3` и трёх израсходованных вызовах всё пошло на `tavily-2`.
- **Не жжёт квоту впустую.** Списание происходит только если движок реально сходил
  к поставщику. SearXNG отвечает 200 и когда движок отвалился — например при
  невалидном ключе Tavily вернёт 401 и попадёт в `unresponsive_engines`; такой вызов
  не засчитывается, а запрос переходит к следующему ключу.

Суммарный дневной потолок складывается: пять ключей по 50 дают 250 запросов в сутки
при 5000 в месяц.

### Ограничения

- Погодные и справочные ответы приходят от `openmeteo` и `wikipedia`, которых нет
  в бесплатном пуле, — за ними нужно обращаться с явным `engines`, либо добавить их
  в `FREE_ENGINES`.
- `bing` в общем пуле заметно шумит, но выдачу не портит: консенсус SearXNG
  опускает одиночные находки вниз.
- `router/packs.json` **генерируется** из шаблона конфига скриптом рендера — руками
  его править не нужно, иначе фильтр разъедется с самими пакетами.

## 9. Настройка

Правки — в **`core-config/settings.template.yml`**, затем:

```bash
python3 scripts/render-settings.py && docker compose restart core
```

`core-config/settings.yml` — сборка, её правки затрутся при следующем рендере, и файл
исключён из git, потому что содержит API-ключи. Так сделано вынужденно: SearXNG не
читает значения настроек из переменных окружения (`environ_name` работает только для
`secret_key` и подобных), а механизма include у него нет — без шаблона ключ лежал бы
в редактируемом конфиге открытым текстом.
Файл правится обычным пользователем благодаря `FORCE_OWNERSHIP=false` в `.env`: без этой
переменной entrypoint образа делает `chown -R searxng:searxng` по смонтированному
каталогу, и `core-config/` переходит к uid 977 — после первого же запуска конфиг
становится доступен только через `sudo`. Ценой этого в логах появляется безобидное

```
!!! WARNING
!!! "/etc/searxng" directory is not owned by "searxng:searxng"
```

Контейнер читает конфиг по правам «для остальных», поэтому каталогу нужен режим `775`,
а файлу — `664` (так и создано). Если владение всё же сбилось, вернуть его можно без
sudo, через сам образ:

```bash
docker run --rm --user 0 -v "$PWD/core-config:/c" --entrypoint sh \
  searxng/searxng:latest -c 'chown -R 1000:1000 /c && chmod 775 /c && chmod 664 /c/settings.yml'
```

Ещё два безобидных сообщения при старте: `missing config file: /etc/searxng/limiter.toml`
(лимитер выключен, файл не нужен) и `ahmia / torch: can't register engine` — это
onion-движки, которым нужен Tor-прокси; на остальную выдачу они не влияют.
Полный список опций: <https://docs.searxng.org/admin/settings/>.

Часто нужное:

```yaml
search:
  default_lang: "ru"      # язык выдачи по умолчанию

engines:                  # выключить шумный движок
  - name: bing
    disabled: true
```

Если поиск стабильно возвращает пустой `results`, а `unresponsive_engines` полон
капч и таймаутов — движки блокируют IP инстанса. Лечится сменой набора движков
(`engines` в settings.yml) или выходом через прокси; наращивать частоту запросов
в этой ситуации бесполезно.
