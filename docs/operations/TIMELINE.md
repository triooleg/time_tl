# Хронология эксплуатации и выпуска RC23

## RC25 admin-only candidate — 2026-10-02, итог20:37Киев

Пользователь запросил публикацию админки. Рабочая RC24 пока не изменена.

- 17:26:09UTC: actual base inventoryPASS, UID10001/site-packages path подтверждены.
- 17:26:34: publicarchive5ea1d2... передан/readbackSHA PASS, secrettransferfalse.
- 17:26:37: pinnedFROM+fourCOPY candidate built, immutable admin digest2edc2a...,
  installed4SHA/Env/User/Entrypoint совпали. Registry толькоlocalhost; не deploy.
- Separateoverlay подписан существующим внешним ключом: e1995... /signature2d822...;
  independentverifyPASS. RC24 rawmanifest6ea.../originalsignature не менялись.
- 17:34:03: new localhostcode Linux61PASS. Runtime/livehost source не устанавливался.
- 17:36:12: exactcandidateSIMULATION35PASS authenticatedsyntheticowner routes,
  no srcshadow/network/productionDB; UUID cleanupPASS. Full943PASS/21skips,
  RuffPASS/mypy77filesPASS. Старые destructivePG E2E против production не запускались.
- 17:37:04: original5RC24IDs/restarts0/rev14/HOLDfalse/source/HB/pulse здоровы.

Candidate independent CODE/SIMULATION/signaturePASS. Publication BLOCKED до exactG8
и independentPASS boundedhandoff/deadman operator. Actualowner UI canary после
восстановления мониторинга обязателен. Approval scope в RC25 APPROVAL.md.

## Каталог и недельное хранение — 2026-10-02, LIVE checkpoint на19:56Киев

Пользователь подтвердил ВСЕ копии за последние7дней; cadence6h не меняется.
Owner UI/settings/selected restore intent готовы LOCAL, actualproductionrestore
не реализован как автоматическая операция и не выполнялся.

- 16:32 UTC: isolated actual PostgreSQL repository51PASS, production не затрагивалась.
- 16:41: actual host persistentpsql CAS/audit/rollback/advisory exclusion1PASS.
- 16:45: Linux host+backup57PASS без skips. Finalfullsuite910PASS/21skips;
  RuffPASS, explicit mypy src/time_tl77filesPASS.
- 16:47: свежая livehealthPASS, пять прежних RC24containers/restarts0/revision14.
- Independent operational review обнаружил гонку общего `flock -n`: поллер
  мог пропустить шестичасовой backup. Сценарий НЕ установлен; добавляются
  boundedwait дляbackup и deterministic contention fixture до liveapproval.
- 16:53:59: final deterministic kernelwaitbarrier/execute fixturePASS, syntheticRAM
  безproductionlock/DB/cron. FixedoperatorSHA5c8ff734... independent operationalPASS.
- 16:54:17: host444first + reviewedbackupatomicreplace + publicrollbackarchivePASS.
- 16:54:20–25: newactualencryptedbackup/isolatedrestore28tables/0008/constraintsPASS,
  artifact SHA0fa5bdde87666a67d831813b3a34efda804a96e349a5f056aab90026aa08fa00,
  3306snapshotrows, rolling7days/deleted0/catalogue3entries.
- 16:54:46: final6hcron-w1800/controller0-6,8-59 installed, exactreadbackPASS;
  two monitors/allunrelatedjobs preserved. No application or secret mutation.
- 16:55:01 и16:56:01: autonomous catalogue generations advanced; mode0444.
- 16:56:22: original5RC24containers/restarts0/rev14/sourcefresh/HB14/pulsePASS,
  HOLDfalse/restorecontainers0/dead37/retry0/open0. Independent B25 LIVE PASS.

Новая панель не опубликована. Следующее приложение требует exact-version approval
и отдельно reviewed migration RC24 identitypins в мониторинге/backup.

## Автоматические бэкапы — 2026-10-02, итог на19:11 Киев

Independent CODE/LIVE PASS; source SHA23beeffd... Exact UTC events: RC24 BACKUP_EVENTS.jsonl.

- 15:53: существующий production env сохранён в DPAPI вне Git; roundtrip PASS.
- Lesson-key отсутствовал в admin: initialcrypt отказала до artifacts/guard changes.
  Existing owner alerts key выбран без правок env/Compose; Linux27 tests PASS.
- 16:05:52–16:05:57: actual encryptedbackup/readback/authdecrypt/isolatedrestore PASS,
  28таблиц/head0008/validatedconstraints/3305rows, disposablecontainer удалён.
- 16:08:43: canarycron установлен; priortransport syntax failures не изменяли cron.
- 16:09:01–16:09:07: unattended cronrestore PASS, artifact SHA256
  `7e8a23ad2a1ff3cacb736094360992d1dfce74197e5c0fae8223cdf62fb9e838`.
- 16:09:58: finalcron `7 */6 * * *` UTC, preservedmonitor/unrelatedjobs, protectedlog600.
- 16:11:24: oneoffhostciphermirror/hash/readback/recoveredDPAPIdecrypt PASS, no plaintextfile.
- 16:11:52: fiveoriginalRC24roleshealthy/restarts0/applied14/freshsource/protectedpulse,
  HOLDfalse/restorecontainers0; no newpreflight/activation/migration/broadcast.

Пользователь добавил требование: админский выбор копии для восстановления и изменение
срока недельного хранения. Новый этап LOCAL, не часть уже проверенного LIVE backupjob.

## Перенос мониторинга на минисервер — 2026-10-02, итог на 18:32 Киев

Пользователь подтвердил размещение на том же `192.168.31.219`, где работает Time TL.
Режим COLOCATED: отказ всей машины не контролируется независимо. Independent PASS.
Машинные события: RC24 `COHOST_EVENTS.jsonl`; приложение/revision 14 не менялись.

- 15:23 UTC: Linux permission probe PASS; переданы только публичный код/config.
- Первый shadow выявил timeout внешнего TLS-маршрута; прежний монитор оставался
  активен. Локальный proxy с проверкой сертификата и public-hostname SNI прошёл.
- 15:27:32: shadow PASS, затем единственный technical route-test оператору,
  initialize и watchdog. Код SHA256
  `28c46135418b72203fec0a1f36d4fd63cd94a203de390e710c50f0c9a34a6793`.
- 15:27:57: старый watchdog снят под flock; установлены два независимых cron-lock.
- 15:28 и 15:30: самостоятельные серверные циклы PASS, HOLD отсутствует.
- 15:30:31: задача RC24 на Windows отключена и сохранена для rollback.
- 15:31 и 15:32: новые cron pulse после отключения ПК; watchdog PASS,
  пять ролей healthy/zero restarts, source fresh, четыре applied heartbeat 14.
  Public admin /login HTTP 200 с русским интерфейсом.

ПК больше не нужен для мониторинга. Пароли, токены и закрытые ключи не менялись
и не переносились. Backup guard 24h сохранён; автоматические бэкапы не добавлены.
Без новой проверенной копии HOLD наступит 3 октября около 17:50 Киев.
G9 пользовательского сценария ДЗ остаётся незавершённым.

## Установка RC24 — 2026-10-02, итог на 17:57 Киев

Машинные события с UTC сохранены в RC24 `DEPLOY_EVENTS.jsonl`; Киев UTC+03:00.

- Пользователь подтвердил точный пакет RC24; independent pre-install PASS.
- 14:49 UTC: подготовлен pending 14 из active 13 с неизменными credentials;
  RC23 Windows task отключён, реальный rule HOLD остановил четыре роли
  sender-first и доставил техническое уведомление только операторскому allowlist.
- 14:50: старый cron watchdog снят под его flock с ожиданием завершения;
  final quiesced encrypted backup SHA и DPAPI round-trip/copy match PASS.
  Изолированное восстановление в network-none/tmpfs PostgreSQL: 28 таблиц,
  head 0008 и все row counts совпали. Production migration не требовалась.
- 14:50:42: ровно один preflight revision 14 PASS; independent receipt review PASS.
- 14:51:56: activation 14 в пределах TTL; 14:51:58 независимый stop timer 300 секунд.
- 14:52–14:53: admin/alert → bot/schedule → sender; между этапами свежий источник
  и здоровье проверены. Пять ролей exact immutable RC24, ноль перезапусков.
- После проверки всех ролей монитор RC24 явно initialized и создал настоящую
  подписанную pulse. Windows Scheduled Task RC24 включён; RC23 остаётся Disabled.
- 14:54:17: новый серверный watchdog/cron PASS; 14:54:20 защитный таймер отменён
  только после строгой проверки pulse, revision, health, source и singleton.
- 14:56–14:57: повторные live checks PASS, Windows task result 0, свежие pulse,
  HOLD false, public admin HTTP 200/Russian text, bot exception summary пуст.

Independent deployment-stages review PASS; полный G9 IN_PROGRESS до настоящего
пользовательского canary ДЗ. Пользователю предложено проверить уникальные предметы
и создание ДЗ через /start. Массовая рассылка, смена паролей/токенов/ключей,
DNS/proxy/firewall/SSH configuration changes не выполнялись.
Временный монитор требует включённый ПК и вход Windows; sleep/shutdown приводит
к безопасной остановке на сервере, а не к автоматическому продолжению работы.

## Подготовка RC24 — 2026-10-02 14:39 UTC (17:39 Киев)

Пользователь потребовал уникальные предметы в ДЗ, сохранив повторы уроков,
и затем поручил деплой после завершения. Оба исправления объединены в новый
RC24 commit `7c4d42784847e621e7e31af83903170b0c7ee0f4`; включены только шесть
проверенных source/test файлов. Push не выполнялся. LOCAL 728 PASS/6 skipped,
Ruff/mypy PASS; независимые CODE review PASS.

Архив передан на сервер, SHA проверена; immutable image построен, pip check и
network-none contract PASS. Новая подпись существующим внешним ключом проверена
локально и exact-image network-none для revision 14. Публичные hash/identity
и события записаны в `.release/time-tl-production-20261002-rc24-homework-subject-fix/`.
Установленный RC23 и active revision 13 пока не изменены. Точный installation
packet, мониторинговое переключение и настоящий ДЗ canary ещё не завершены.
Пароли, токены и ключи не менялись.

Независимый RC24 CANDIDATE packet review: PASS (Git identity, шесть source/test
файлов, detached public-key signature, exact-image event). INSTALL остаётся
BLOCKED до G8 точного подтверждения версии и review исполняемого мониторингового
переключения. План исправлен: startup под независимым server-side stop timer
пять минут, затем новая настоящая pulse/cron/task; таймер отменяется только после
watchdog PASS. Старый RC23 watchdog не должен остановить новый стек.

## Актуализация: установка и незавершённый canary, 2026-10-02

Время ниже UTC (Киев +03:00). Источник фактических событий:
`.release/time-tl-production-20261002-rc23-homework-menu/DEPLOY_EVENTS.jsonl`.

- 14:03–14:04: rule-triggered HOLD и server missing-pulse watchdog остановили
  четыре роли sender-first; технические уведомления только операторскому allowlist.
- 14:05: финальный quiesced DPAPI backup; локальная/серверная encrypted SHA совпали.
- 14:06: миграция 0008 и ровно один production preflight revision 13 — PASS.
- 14:08: однократная activation revision 13 после независимого PASS, receipt
  использован в пределах TTL. Пароли, токены и signing keys не изменялись.
- 14:09–14:12: admin/alert, затем bot/schedule, sender последним.
- 14:13–14:14: временный Windows Scheduled Task и серверный cron watchdog включены.
- 14:24: свежий live check — пять ролей exact RC23 healthy, restarts 0,
  heartbeat revision 13, один preflight/activation, watchdog PASS, HOLD false.
  Публичная админка HTTP 200, русский текст; monitor last PASS 14:24:00 UTC.
- Пользователь подтвердил /start и новое меню, затем сообщил, что текст ДЗ
  остаётся без ответа. Sanitized live logs: четыре KeyError в receive_text.
  LOCAL assembled Dispatcher воспроизвёл потерю subject_id в BoundedMemoryStorage.
  Исправление прошло LOCAL: 714 PASS/6 destructive PG skipped, independent CODE
  PASS/75 rerun tests; patched image ещё не установлен. Git commit/push не выполнены.

Итог: RC23 установлен, но полный G9 BLOCKED до исправления и реального canary ДЗ.
Предыдущие записи о неустановленном RC23 ниже сохранены как история, не текущий статус.

## Дополнение: независимая перепроверка мониторингового модуля

Записано 2026-10-02, точное время операций здесь не восстановлено. Первый набор
из 16 тестов не закрыл независимое замечание: задержка между замером и подписью
не увеличивала возраст источника. Автор исправил учёт задержки для источника,
heartbeat, backup, открытого outbox и TLS; тесты стали использовать валидные
пороговые настройки и route fixture с проверкой причины отказа. Оркестратор
повторил 18 тестов и Ruff: PASS. Независимый reviewer затем подтвердил PASS
исправления возрастов замеров; INSTALL сохранил BLOCKED. Это LOCAL проверка, не постоянно работающий
мониторинг и не разрешение на production installation. Автоматическая цепочка
threshold → HOLD → sender-first stop → уведомление остаётся недоказанной.

Запись составлена: `2026-10-02 13:28:48 UTC`, помощник `key_evidence_custodian`.
Время записи не является временем события. Для событий без точной исходной метки
указана только дата; точное время не восстанавливалось по предположению.
Инструкции: [POLICY.md](POLICY.md); метаданные ключей: [KEY_REGISTER.md](KEY_REGISTER.md).

## Неизменяемая идентичность кандидата

| Поле | Значение |
|---|---|
| Tag | `time-tl-production-20261002-rc23-homework-menu` |
| Tag object | `d412329dcbf0662de73a719d4cf36107dc007975` |
| Commit | `7206246abd84bd8dad4dd8e2afccafdf1dc790d3` |
| Tree | `97496fe01edacf37eb49db646f3697494e0b28f6` |
| Archive SHA-256 | `baf4a4be401c46a470c3c545d5c2f5609d4301cc66731edb8d48b6a45e70087e` |
| Compose SHA-256 | `6412610bc52f2ca302cee2f656d2a64696a8e90f36d1b407af7d46bddbb3a742` |
| Immutable image | `localhost:5000/time-tl-bot@sha256:5661566f8d9c1536a4a950c8e521c88a6ea0e47b98996eeb27d443c0e8390238` |
| Требуемая миграция | `0008_homework_foundation` |
| Capability | `homework_enabled=true` |

## События и границы доказательств

| Исходное время | Исполнитель / среда | Действие, основание | Наблюдаемый результат / источник | Ограничение |
|---|---|---|---|---|
| 2026-10-02, точное время не записано в источнике | Оркестратор и независимый safety reviewer / LOCAL | Подготовка и проверка RC23 по поручению пользователя завершить деплой | MANIFEST: 682 passed, 6 skipped; Ruff, mypy 76 файлов, compileall, diff check PASS; независимый review исходников и упаковки PASS | Не production PASS; шесть DB E2E пропущены |
| 2026-10-02, точное время не записано в источнике | Оркестратор / сервер подготовки | Передача архива, проверка SHA, build и push в существующий локальный registry | SERVER_PREPARATION: SHA совпали; immutable digest зарегистрирован; `--network none` image contract и `pip check` PASS | Не полная Linux test suite и не CVE scan; production контейнеры не заменялись |
| 2026-10-02, точное время не записано в источнике | Оркестратор / PRODUCTION, read-only | Инвентаризация перед деплоем | PRODUCTION_PLAN: роли здоровы, рестарты 0, прежний RC19 image, DB `0007_outbox_indeterminate`; STAGING приложения не запущены | Снимок состояния, не live command E2E и не гарантия последующего состояния |
| Идентификатор backup `20261002T131442Z`; точное время завершения репетиции не записано | Операторский backup helper / PRODUCTION read-only и изолированная лаборатория | Зашифрованный backup, проверка независимой копии, restore и миграционная репетиция | BACKUP_REHEARSAL JSON: PASS, 24 таблицы; upgrade `0007→0008`, downgrade и сохранение существующих counts PASS; `production_changed=false` | Production оставалась доступна для записи; нужен финальный maintenance backup |
| 2026-10-02, точное время не передано | Оркестратор / LOCAL | Найдены ранее существовавшие внешние signing keys, проверено соответствие доверенным публичным ключам | Контекст оркестратора: два результата `PUBLIC_KEY_MATCH`; зарегистрированы публичные метаданные | Не новое создание; ACL и recovery не проверены; независимый receipt пока не приложен |
| 2026-10-02, точное время операции не зафиксировано | Оркестратор / LOCAL, metadata-only | Проверка существования и Get-Acl двух signing files | Сообщение оркестратора: файлы существуют, оператор имеет FullControl Allow, наследование включено | Не hardening и не recovery PASS; содержимое файлов не читалось этой проверкой, ACL не изменялись |
| 2026-10-02, точное время сообщения не извлекалось | Пользователь / управление проектом | Поручил документировать события и назначить отдельного помощника по учёту ключей | Прямое пользовательское сообщение; создан настоящий task packet OPS-CUSTODY-01 и эти документы | Помощник не является постоянным фоновым монитором или secret vault |

## Репетиционный backup: метаданные без содержимого БД

- Локальный файл: `.release/time-tl-production-20261002-rc23-homework-menu/protected-backups/production-20261002T131442Z.dump.dpapi`.
- Серверная зашифрованная копия: `/home/trio/time-tl-releases/rc23-production-20261002T131442Z.dump.dpapi`.
- SHA-256: `28e99033e9deb499801e4c752e971acafdf29746aea2bc0a721a5fffdf88065c`.
- Проверка совпадения копий: `true` в aggregate receipt.
- Лаборатория: `time-tl-rc23-rehearsal-20261002t131442z`; остановка с сохранением
  volume сообщена оркестратором, не проверялась хранителем на сервере.
- DPAPI decrypt требует того же Windows операторского аккаунта; потеря аккаунта
  или станции не покрыта независимым recovery evidence.

## Обновление OPS-CUSTODY-02

Время внесения записи: `2026-10-02 13:49:16 UTC`. Следующие результаты переданы
оркестратором; хранитель не выполнял remote checks и не открывал секреты.

- Production pending revision установлен: **13**, active остаётся **12**.
- Создана подпись нового RC23 manifest **существующим** внешним ключом.
  SHA-256 raw manifest: `38b2073df297d9b02b4564d02a56aabbb2dd43c1e7996196360f64bdd1383865`;
  semantic fingerprint: `b169feed35bb7e8c25d54a6688127460877d94a2d0e86d080f8312ded5835ff8`.
  Независимый signature reviewer сообщил `PASS`; точное время подписи не передано.
- Публичный manifest и подпись размещены в
  `/home/trio/time-tl-releases/rc23-operator-packet`. После исправления traversal
  permissions публичных файлов (каталог `755`, файлы `444`) exact-image validation
  без сети сообщил `PASS`. Закрытые ключи на сервер не копировались.
- Подготовлен защищённый Compose env копированием существующих credentials без их
  изменения. Rendered Compose: все application роли используют digest `5661566f…`,
  homework включён только у bot, owner revision `13`. Это подготовка, не установка.
- `2026-10-02T13:46:52.420180+00:00`: доставлен **один прямой** notification-route
  test существующему allowlist из одного аккаунта; receipt hash
  `081e20fa7a536337b5aa7548bcf33e78b913ff4dbf00b2a121095a27da9fcb83`.
  Он подтверждает маршрут доставки, **не** срабатывание threshold/rule и не
  автоматическую остановку sender.
- `2026-10-02T13:47:04.725834+00:00`: реальный одиночный monitoring sample:
  provider age `12.99 s`, circuit `false`, failures `0`, max heartbeat age `14.47 s`,
  open outbox `0`, historical dead `37`, retry `0`, attempts `0`, restarts `0`,
  disk free `44.585%`, memory available `63.237%`, TLS remaining `81.13 days`.
  Historical dead count не трактовать как 37 новых ошибок; sample не доказывает
  непрерывное наблюдение или автоматическую реакцию.
- Независимый INSTALL review: **BLOCKED** — отсутствуют непрерывный внешний
  collector и доказанная цепочка `threshold → HOLD → sender stop → notification`.
  Прямой route test не закрывает rule-test.
- Исправлена проблема stale signing в off-host builder; `16 tests PASS` сообщены
  оркестратором. Независимый review исправления ещё не завершён.
- Production stop, migration, preflight и activation **не выполнялись**.
  Процедура подписи сохранена в [SIGNING.md](SIGNING.md). Ключи не создавались,
  не ротировались и не переносились; пароли не менялись.

Доступные локальные ссылки: [operator_control.py](../../.release/time-tl-production-20261002-rc23-homework-menu/operator_control.py),
[release-manifest.json](../../.release/time-tl-production-20261002-rc23-homework-menu/release-manifest.json),
[monitor sample](../../.release/time-tl-production-20261002-rc23-homework-menu/monitor-sample-2026-10-02T134704.725834_0000.json).
Наличие файла установлено по inventory; содержимое этих новых receipts хранитель
не проверял. Reviewer verdicts выше — атрибутированные сообщения оркестратора,
не независимые заключения хранителя.

## Незакрытые шаги после обновления

Статус установки: `BLOCKED` независимым INSTALL review; подготовительные работы
продолжаются, публичное обновление RC23 **не подтверждено**.

1. Завершить непрерывный внешний monitoring и доказать автоматическую цепочку
   threshold/HOLD/sender-stop/notify с независимым PASS.
2. Проверить исправление off-host builder независимо и подготовить свежие
   monitoring receipts; pending `13` и новый signed RC23 manifest уже подготовлены.
3. Получить независимую проверку финального пакета, singleton ролей, mount и rollback.
4. После остановки writers создать и проверить финальный maintenance backup.
5. Выполнить production migration с проверкой head, ровно один preflight,
   независимый PASS, activation в пределах срока receipt.
6. Запустить роли в проверенном порядке; canary только операторскому allowlist,
   без массовой рассылки; записать реальные результаты команд и мониторинга.

Общее поручение пользователя завершить деплой записано; оно не заменяет независимые
release gates. Новые строки добавлять после действий с UTC, точной версией, результатом
и receipt. Не трактовать планы, summary, health-only или simulation как production E2E.

## RC25 опубликован — 2026-10-02 18:06:50 UTC / 21:06:50 Киев

Точный admin-only пакет из APPROVAL.md установлен после независимого PASS.
Новый admin image digest2edc; четыре RC24 bot/worker контейнера и revision14
сохранены без перезапуска. Final handoff SHA dfdb3e4f...; run678e0a2a... PASS.
Root наблюдал два автономных pulse/catalogue цикла 18:07 и18:08 и штатный
шестичасовой backup18:07:01→06.996 с реальным isolated restore28tables/constraints/
schema0008 PASS. Пять копий, все за rolling7days, удалено0. Пароли/ключи неизменны,
production restore/Telegram test/migrations/preflight/activation не выполнялись.
Independent LIVE operational PASS. Owner authenticated UI остаётся REVIEW/PENDING.
Public login200/backups303/api401 — только anonymous reachability/auth protection.
Подробности/receipts: .release/time-tl-production-20261002-rc25-backup-admin/LIVE_PUBLICATION.md.
Будущие Compose действия обязаны сохранять RC25 admin-override.json; исходный
BASE Compose без override вернёт старую админку и вызовет корректный HOLD.

## RC25 принят владельцем; новая концепция — 2026-10-02 20:18 UTC / 23:18 Киев

Пользователь сообщил «1. проверил все работает. давай дальше». Проверка админки
принята по сообщению владельца; это не дополнительная серверная проверка и не
восстановление production-БД. Начат LOCAL этап12: ADR0012, чистая проекция
календарей и выключенный по умолчанию экран «Мій день» из текущего активного
расписания. Подписки/роли/новая схема БД остаются следующим этапом13.
Пароли, ключи, production/staging, рассылки и развернутый RC25 не изменялись.

## Этап12 завершён локально — 2026-10-02 20:33 UTC / 23:33 Киев

Реализованы чистая проекция нескольких календарей и экран «Мій день», который
пока читает только существующее активное личное расписание. Флаг MY_DAY_ENABLED
по умолчанию false. Root: полный pytest1086PASS/22SKIP (Linux и изолированный PG),
RuffPASS, mypy79filesPASS. Независимый safety-review C12-04: LOCAL PASS, отдельно
перезапущены98 тестов новой функции и133 старых safety/simulation теста.
Учтены пересечения/повторные уроки/Киев-DST, недоступность данных, права владельца
и совместимость FSM добавления ДЗ при выключенной функции. Подписки и сохранение
новых календарей требуют этап13. Сервер, RC25, пароли/ключи и рассылки не менялись.
Подробности: .planning/phases/12-calendar-day-projection/12-VERIFICATION.md.

## Этап13-01: фундамент календарей — 2026-10-02 20:58 UTC / 23:58 Киев

LOCAL реализованы отдельные календари, роли/подписки, адресные hash-only приглашения
с preview/confirmation/TTL24h, CAS/лимиты, собственная видимость/выход и удаление
владельцем. Создание из старых профилей не автоматическое; миграция0009 подготовлена,
не выполнялась. Календарное чтение defaultOFF, fail-closed при недоступности источника;
уведомления/alerts/workers/outbox не менялись. Предупреждения об удалении аккаунта
объясняют владельцу потерю общих календарей для всех участников.

Root1264PASS/23SKIP, RuffallPASS/mypy81PASS; независимый C13-S LOCAL PASS после218
focusedtests и отдельного runtime теста. Исправлены сосуществование приглашений,
FK-схема/параметры индексов и точность сравнения SQL-литералов/defaults/grouping.
История:13-01-REVIEW.md; контрольные хэши и границы:13-01-VERIFICATION.md.
Релиз REVIEW: actual disposablePG/migrations/concurrency/cascades и backup/restore
head0009 ещё не проверены; RC25 verifier остаётся0008. UI управления/overrides далее.
Сервер/RC25/пароли/ключи/Telegram/Git не изменялись; пакет на деплой не создавался.

## Этап13-02 / кандидат RC26 — 2026-10-02 21:32 UTC / 2026-10-03 00:32 Киев

По запросу «делай и после этого деплой» реализованы приватное Telegram-управление
календарями, создание подтверждённого знимка, видимость, адресные приглашения,
выход/удаление с подтверждением, JSON-экспорт и boundedочистка истёкших приглашений.
Исправлены выявленные независимым review гонка лимитов и сохранение calendarFSM
после /start,/menu/главного меню. Root1354PASS/23SKIP; RuffPASS/mypy82PASS;
независимо437focusedPASS, actual frozenPG14checksPASS и восстановление старой0008
и новой0009 зашифрованных копий в изолированной tmpfsБД. Ключи/формат не менялись.

Точный кандидат: archive ae0a715a6eeddd0e54bbcf45e3bb23b0ba2277e75087eb46a0f508a4a7da9e37,
image sha256:5d0c094d90bf8c0940f94a5ca10b357ef2322cdfc9c2f339f51a7c63dcca4728.
Публичная подпись существующим внешним ключом подтверждает candidateprovenance,
а не runtimeattestation/одобрение/деплой. НовыйGitcommit не заявлен. Кандидат помещён
только в существующий локальный registry сервера; временная лаборатория удалена
после проверки её точных ID/labels. РабочийRC25, active14/head0008, секреты,
SSH-настройки и Telegram-рассылки не изменены. Следующий шаг — точныйG8 и план
RC26_RELEASE_PLAN.md; productionмиграция/preflight/activation/canary ещё не выполнены.

## Источники и актуальность

Источники ниже прочитаны хранителем 2026-10-02; временные `.release` receipts могут
быть недоступны в чистом checkout. Они подтверждают только указанный уровень проверки.

- [MANIFEST.md](../../.release/time-tl-production-20261002-rc23-homework-menu/MANIFEST.md): LOCAL source identity/tests; его ранняя release-boundary запись предшествует server build.
- [SERVER_PREPARATION.md](../../.release/time-tl-production-20261002-rc23-homework-menu/SERVER_PREPARATION.md): image build/probe; перечень remaining checks предшествует backup rehearsal.
- [PRODUCTION_PLAN.md](../../.release/time-tl-production-20261002-rc23-homework-menu/PRODUCTION_PLAN.md): план и read-only snapshot; ранняя фраза о непроверенном backup не отражает последующий rehearsal.
- [BACKUP_REHEARSAL_20261002T131442Z.json](../../.release/time-tl-production-20261002-rc23-homework-menu/BACKUP_REHEARSAL_20261002T131442Z.json): aggregate restore/migration evidence, без пользовательских данных в этой хронологии.
- Переданный контекст оркестратора: обнаружение signing keys, public matching и остановленная лаборатория; не независимое доказательство хранителя.
