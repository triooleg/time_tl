# Настройка и подключение Time TL

## Статус на 2026-09-23

Бот, PostgreSQL-репозитории, alert/schedule/sender workers и административная
панель реализованы и проверены локальными тестами. Развёртывание не выполнялось.
Production намеренно заблокирован capability gates: нет одобренного георезолвера,
production-разрешения для областного provider scope, staging evidence и разрешения
на точную версию.

Сейчас пользователь может вручную выбрать и подтвердить область. Отправленная
Telegram-геолокация не преобразуется в регион: resolver возвращает отказ, а сырые
координаты не сохраняются. Не называйте это готовой геолокацией.

## Что потребуется для staging

- отдельный Telegram-бот от `@BotFather`;
- отдельный токен alerts.in.ua для Time TL;
- PostgreSQL 16;
- стабильный Fernet-ключ для ссылок на уроки;
- стабильный ключ хеширования IP панели длиной не менее 32 байт;
- утверждённый список тестовых Telegram chat ID;
- HTTPS и проверенная trusted-proxy policy для панели;
- immutable image с записанными commit, digest и Alembic revision.

Не переносите токены, SSH-пароли или пароли БД из других проектов. Не публикуйте
секреты в Git, чатах, логах, screenshots, build args или документации.

## Официальные тревоги

Единственный серверный `alert-worker` обращается к официальному endpoint:

`https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json`

Ответ — JSON-строка ровно из 27 символов в официальном порядке. `A` означает
тревогу по всей области, `P` — тревогу в части районов/громад; оба значения считаются
ACTIVE для выбранной области. `N` допускается только после полной строгой проверки.

Защита от лимитов и ложного отбоя:

- один poller для всех пользователей, а не запрос на каждого ученика;
- интервал не меньше 15 секунд, нормальная частота — до 4 запросов в минуту;
- отдельный лимит 5 запросов в минуту без накопления burst;
- `If-Modified-Since` используется только с проверенным baseline;
- snapshot, outbox, fetch revision и `Last-Modified` фиксируются атомарно;
- response body читается потоково и ограничен 4096 байтами;
- timeout, `401/403`, `429`, `5xx`, неверная длина/символ, stale/partial response
  никогда не означают отбой.

## Локальная установка

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Для безопасной локальной симуляции используйте только заглушки:

```dotenv
TIME_TL_ENV=simulation
ALERT_PROVIDER=fake
TELEGRAM_TRANSPORT=disabled
ALLOW_SCAFFOLD_SERVICES=true
ADMIN_BACKEND=local_sqlite
ADMIN_COOKIE_SECURE=false
ALERT_POLL_INTERVAL_SECONDS=15
ALERT_STALE_AFTER_SECONDS=120
ALERT_RATE_LIMIT_PER_MINUTE=5
ALERT_RATE_LIMIT_BURST=1
SENDER_TRANSPORT_TIMEOUT_SECONDS=8
```

`DATABASE_URL`, `POSTGRES_*` всё равно должны содержать согласованные локальные
значения для Compose. `.env` не должен попадать в Git.

## Локальные проверки

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe --strict src
.\.venv\Scripts\python.exe -m compileall -q src tests migrations
.\.venv\Scripts\alembic.exe upgrade head --sql
node --check src/time_tl/admin/static/admin.js
docker compose config
```

Записанное 2026-09-23 evidence: pytest 241 PASS + 1 opt-in PostgreSQL E2E SKIP,
unittest 233 tests + тот же 1 SKIP, Ruff PASS, strict
mypy PASS для 66 source files, compileall PASS, Alembic offline PASS, Compose
восемь services, JavaScript syntax PASS. Это LOCAL evidence, а не доказательство
работы контейнеров или сервера.

## Миграции

Миграции запускаются отдельным профилем и не стартуют одновременно во всех workers:

```bash
docker compose --profile ops run --rm migrate
```

Перед staging обязательны backup, проверенный restore в отдельную БД, фиксация
исходной/целевой Alembic revision и тест повторного безопасного запуска. Offline SQL
generation не заменяет реальную миграцию PostgreSQL.

## Runtime services

Compose описывает восемь services: `bot`, `alert-worker`, `schedule-worker`,
`sender-worker`, `admin-web`, `migrate`, `node-exporter`, `postgres`.

PostgreSQL adapters, реальные schedule/sender loops, transactional outbox,
heartbeats и агрегаты панели присутствуют. Однако production capability flags для
schedule/sender всё ещё закрыты. Не включайте их вручную: флаг открывается только
после PostgreSQL restart/fault evidence и независимого safety review.

Для sender задан ограниченный transport timeout:

```dotenv
SENDER_TRANSPORT_TIMEOUT_SECONDS=8
```

Допустимый диапазон — больше 0 и не более 10 секунд. Compose явно передаёт эту
переменную через общий environment; release default — 8 секунд.

## Подготовка staging

Staging допускается только с отдельным ботом и allowlist:

```dotenv
TIME_TL_ENV=staging
OWNER_CONFIG_SOURCE=database
OWNER_TELEGRAM_FERNET_KEY=SECRET_FERNET_KEY_FOR_TELEGRAM
OWNER_ALERTS_FERNET_KEY=DIFFERENT_SECRET_FERNET_KEY_FOR_ALERTS
DATABASE_URL=postgresql+asyncpg://...
LESSON_LINK_FERNET_KEY=SECRET
ADMIN_BACKEND=postgres
ADMIN_IP_HASH_KEY=SECRET_AT_LEAST_32_BYTES
HOST_METRICS_URL=http://node-exporter:9100/metrics
ADMIN_COOKIE_SECURE=true
ALLOW_SCAFFOLD_SERVICES=false
SENDER_TRANSPORT_TIMEOUT_SECONDS=8
```

Telegram Bot Token, alerts.in.ua token, poll/stale и staging allowlist после
bootstrap вводятся только в owner panel. Сохраните draft и запросите активацию.
Оператор сверяет exact version/evidence, активирует конкретную revision и
контролируемо перезапускает worker-контейнеры. Панель никогда не показывает токены
повторно и не активирует реальную доставку одним сохранением формы.

Наличие переменных не открывает закрытые capability gates. До G6 нужно:

1. выполнить real PostgreSQL fault/restart сценарии;
2. получить независимый safety verdict;
3. одобрить resolver координат или официально ограничить staging ручным выбором;
4. проверить allowlist на каждом outbound path;
5. проверить onboarding, тревогу, два подтверждения отбоя, возврат на текущий урок,
   retry/dedupe и restart;
6. убедиться, что ни один неразрешённый chat ID не получил сообщение.

## Production и выдача данных доступа

Production запуск возможен только после G8 для точных commit/image digest/Compose и
после G9 canary. Текущие значения capability `production_oblast_scope_release` и
`reviewed_region_resolver` — false, поэтому публичный запуск должен fail closed.

Логин панели — `admin`. Пароль заранее не существует и не должен быть придуман в
документации. Одноразовый пароль появляется только после утверждённого bootstrap,
сохраняется в защищённый artifact, имеет срок действия и удаляется после смены или
истечения. URL, пароль и срок действия передаются владельцу только после фактической
проверки TLS и bootstrap.

Развёртывание, создание production credentials и отправка реальным пользователям
на текущем этапе не выполнялись.
