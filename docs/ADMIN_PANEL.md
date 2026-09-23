# Управление Time TL через административную панель

## Что реализовано

Панель имеет локальный SQLite backend для разработки и асинхронный PostgreSQL
backend для staging/production. PostgreSQL backend собирает агрегаты пользователей,
режимов, активных профилей, outbox, provider state и worker heartbeats. Host CPU,
RAM, disk, load, network и uptime запрашиваются у отдельного read-only
`node-exporter`; Docker socket панели не передаётся.

Dockerfile включает `docs/`, поэтому встроенные ссылки на `SETUP_BOT.md` и этот
файл доступны в container image. Панель запущена на мини-сервере только в режиме
SIMULATION: PostgreSQL и healthchecks проверены, `node-exporter` работает, а порт
панели доступен только на server loopback `127.0.0.1:8081`. Это не staging и не
production.

## Доступ и bootstrap

- логин: `admin`;
- заранее заданного пароля нет;
- одноразовый пароль создаётся только командой `bootstrap-admin` после применения
  миграций и записывается в защищённый файл;
- пароль не печатается в logs/stdout и не хранится в environment;
- bootstrap действует 24 часа;
- первый вход требует заменить пароль; artifact удаляется после смены или expiry.

Пример локального bootstrap:

```powershell
New-Item -ItemType Directory -Force .secrets | Out-Null
icacls .secrets /inheritance:r /grant:r "$env:USERNAME`:(OI)(CI)(F)"
time-tl bootstrap-admin --output .secrets\admin-bootstrap.txt
```

Для PostgreSQL/Portainer точная one-off команда утверждается вместе с image digest
и volume mapping. Нельзя подставлять предполагаемое имя volume или выводить файл
через `docker logs`, `cat`, Portainer console history либо screenshot.

После фактического bootstrap владельцу отдельно передаются:

```text
Панель: VERIFIED_HTTPS_URL
Логин: admin
Одноразовый пароль: VALUE_FROM_PROTECTED_ARTIFACT
Действует до: VALUE_FROM_ARTIFACT
```

Сгенерированный пароль намеренно не приводится в документации. Получайте его только
из защищённого bootstrap artifact на сервере и удалите artifact после обязательной
смены пароля.

## Как открыть панель

Панель SIMULATION не опубликована в локальную сеть или Интернет. На доверенном
компьютере, имеющем доступ к мини-серверу, запустите:

```bash
ssh -N -L 8081:127.0.0.1:8081 trio@<MINI_SERVER_HOST>
```

Не закрывая SSH-сеанс, откройте `http://127.0.0.1:8081` и войдите как `admin`.
SSH-пароль и пароль панели в эту команду, документацию, чат или скриншоты не
добавляются. Для будущего внешнего доступа сначала нужны проверенные HTTPS,
reverse proxy и точные `ADMIN_TRUSTED_PROXY_CIDRS`.

## Защита входа

| Контроль | Реализация |
|---|---|
| Хеш пароля | Argon2id |
| Новый пароль | минимум 14 символов и встроенный denylist |
| Неудачные входы | 5 попыток за 15 минут по account + хешированному source IP |
| Блокировка | 30 минут |
| Сессия | idle 30 минут, absolute 12 часов |
| Cookie | `HttpOnly`, `SameSite=Strict`, `Secure=true` вне local/simulation |
| Изменяющие формы | CSRF token |
| Browser policy | CSP, `frame-ancestors 'none'`, `X-Frame-Options: DENY`, no-store |

Uvicorn proxy headers отключены. Если панель стоит за reverse proxy, source IP
принимается только через явный `ADMIN_TRUSTED_PROXY_CIDRS`; список должен содержать
только проверенные proxy addresses. Пустой или неверный список не следует обходить.

## Данные панели

### Обзор и пользователи

Показываются только агрегаты: количество пользователей, распределение режимов,
число активных профилей, импортов и записей outbox. Telegram ID, координаты,
индивидуальное расписание и ссылки на уроки в панель не выводятся.

`null`/«недоступно» не преобразуется в ноль: отсутствие данных не должно выглядеть
как отсутствие проблем.

### Провайдер

Показываются сохранённые fetch revision, `last_success_at`, circuit/backoff,
состояния областей и текущие poll/stale настройки. UI дополнительно пересчитывает
freshness; отсутствующий или неверный timestamp отображается как `UNKNOWN/STALE`,
никогда как отбой.

- `ACTIVE` — тревога применима к области;
- `CLEAR_CONFIRMED` — отбой подтверждён валидной последовательностью;
- `UNKNOWN` — безопасного решения нет;
- `STALE` — сохранённые данные больше нельзя считать свежими.

### Доставка

PostgreSQL агрегаты включают pending/delivered/dead/retrying и возраст старейшей
записи. Кнопка pause/resume остаётся отключённой: полноценный re-authenticated
операторский flow ещё не является разрешённым production control. Не выдавайте UI
за возможность безопасно остановить или возобновить доставку.

### Сервер и воркеры

Worker processes записывают heartbeat в PostgreSQL. Панель разрешает только имена
`bot`, `alert-worker`, `schedule-worker`, `sender-worker` и санитизированные статусы;
instance IDs и свободные details не раскрываются.

`node-exporter` подключён в Compose к отдельной internal metrics network и read-only
host mounts. В SIMULATION он запущен; перед staging всё равно нужно сопоставить
filesystem/memory/CPU значения с самим хостом и повторно проверить, что exporter не
доступен извне.

### Настройки

Форма сохраняет в PostgreSQL:

- poll interval: 15–300 секунд;
- stale threshold: не меньше `2 × poll` и не больше 3600 секунд.

Alert worker читает эти настройки на следующем цикле. Настройки не позволяют
увеличить rate limit, burst или число alert-worker replicas и не показывают token.
Каждое изменение должно оставаться в audit trail.

## Встроенный чек-лист «Настройка проекта»

На главной странице панели есть операторский чек-лист. Он перечисляет только
названия параметров и порядок проверки; значения секретов панель не читает и не
показывает. Владелец должен подготовить:

1. отдельного Telegram-бота от BotFather и `TELEGRAM_BOT_TOKEN`;
2. отдельный токен alerts.in.ua в `ALERTS_IN_UA_TOKEN`;
3. для staging — только согласованные числовые ID в `TEST_CHAT_ALLOWLIST`;
4. пароль PostgreSQL и согласованный `DATABASE_URL`;
5. стабильные `LESSON_LINK_FERNET_KEY` и `ADMIN_IP_HASH_KEY` (не менее 32 байт);
6. HTTPS, secure cookie и точные CIDR проверенного reverse proxy;
7. ручной выбор и явное подтверждение области, пока георезолвер не одобрен;
8. проверенный активный профиль расписания, пропуски уроков, парность,
   рабочие субботы и исключения дат;
9. свежесть provider state, worker heartbeats, outbox и метрики сервера после
   запуска.

Секреты добавляются только в защищённое окружение сервера. Их нельзя вводить в
страницу панели, сохранять в Git, пересылать в чатах или показывать в логах и
скриншотах. `SIMULATION`, `STAGING` и `PRODUCTION` в чек-листе разделены:
simulation не отправляет реальные сообщения, staging использует отдельного бота и
allowlist, production требует G8/G9 и явного схвалення точної версії.

## Ежедневная проверка после разрешённого запуска

1. Проверьте environment badge и время генерации данных.
2. Проверьте `last_success_at`, freshness, circuit/backoff и `UNKNOWN/STALE`.
3. Проверьте один heartbeat на каждый ожидаемый worker.
4. Проверьте pending/retrying/dead и возраст самой старой outbox записи.
5. Сопоставьте CPU/RAM/disk/load с host exporter.
6. Проверьте срок и результат последнего backup/restore rehearsal.
7. При сомнении не выставляйте отбой вручную и не очищайте outbox/volume.

## Аварийные границы

- Restart панели не доказывает свежесть provider state.
- Потеря provider connection запрещает новый all-clear и возврат на урок.
- `401/403` требует исправления секрета и отдельного operator resume.
- `429` требует `Retry-After`/backoff, а не дополнительной replica.
- Health endpoint доказывает только liveness процесса.
- При заполнении disk сначала остановите рискованные операции и сохраните данные;
  PostgreSQL volume не удаляется.

## Текущий release status

Для exact commit `4ea3b1bedfc31142ae2c53d32e550e4e8a20dcff` проверены
SIMULATION stack start, migration `0003`, PostgreSQL restart/fault E2E,
backup/restore, restart persistence и loopback-доступ к панели. Используются fake
provider и отключённый Telegram; реальных пользователей и токенов нет. G2-G5
пройдены только для SIMULATION. G6-G9, Telegram staging, HTTPS и production URL
остаются заблокированными.
