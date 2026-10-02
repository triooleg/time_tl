# Реестр ключей и ответственности

## RC25 admin overlay — 2026-10-02, подготовка

Публикация 18:06:50 UTC / 21:06:50 Киев: прежняя подпись проверена на сервере,
overlay установлен read-only в новой админке; никаких новых ключей, ротации,
изменения паролей или production env не было. Original RC24 attestation сохраняется.
Independent operational PASS; owner authenticated UI остаётся REVIEW.

Отдельная admin-only overlay attestation подписана прежним внешним Ed25519 release
PEM; новых ключей, копирования закрытого файла и ротации не было. Ключ читался
только локально в RAM. Original RC24 runtime manifest не перезаписывался и не
объявляется подписью нового admin image. Overlay отдельно связывает exact base,
четыре source SHA, archive SHA и новый immutable admin image, revision14/schema0008.
Admin overlay raw SHA256 e1995a0a4eeaaa7fc7bde72a394362df4fd711d6cfcb9aa5b526ae855a1d1b21;
signature SHA256 2d822fd48d62bb6d5f7f72e167c106f0c5452fdf135484e06cc1e9e9c39488ba.
Подпись не является G8 разрешением на установку. Отдельное G8 получено;
production admin теперь RC25, четыре bot/worker роли остаются RC24/revision14.

## Автоматические бэкапы — 2026-10-02 16:11 UTC

Новых постоянных ключей не создано, пароли/ключи не ротировались. Purpose-separated
HKDF-SHA256/AES-256-GCM derived key вычисляется из существующего OWNER_ALERTS_FERNET_KEY
только в admin RAM. Случайный nonce для каждой копии — не новый постоянный ключ.
Existing key не выводится в argv/stdout/logs и не копируется в public monitor config.
Исходный production env с тремя прежними Fernet keys защищён Windows DPAPI вне Git:
`D:\CodexData\release-artifacts\time-tl\rc24-automatic-backups\rc24-production-environment-recovery.dpapi`.
DPAPI roundtrip PASS; ciphertext SHA256
`8b0003dfb140d1d20e712a6b668f29e3f9a08e675dd6a9dd3e7b852b5e2aa94b`.
Одна backup copy туда зеркалирована, recovered env действительно расшифровал её
в RAM без plaintext-файла. Это текущий Windows профиль, не portable DPAPI recovery.
Регулярного offsite mirror пока нет; release/monitor signing keys не изменялись.

## Перенос мониторинга — 2026-10-02 15:32 UTC

COLOCATED monitor установлен на существующем сервере без создания/ротации ключей.
Переданы только публичные код, thresholds, baseline, identity и дата проверенного
backup. Закрытые PEM, SSH credentials, Telegram tokens и DPAPI config не переносились.
Токен технического уведомления читается существующим admin runtime только в памяти;
получатель — существующий операторский allowlist из одного аккаунта.
Windows monitoring task/config сохранены отключёнными для rollback.
Неподписанная local pulse не является signed offhost release evidence.

## Актуализация RC24 — 2026-10-02

Новый RC24 manifest подписан существующим release PEM без создания/ротации ключа.
Manifest raw SHA256 `6ea0c49142f62dec148ce9eb9751eba58286e79efc1c2d9c46fb603766c6c0ff`,
signature SHA256 `df1b637ba391f77b99dff50dcb4f187ec1cfa9609ceacdf48fed1f93fcb4e60c`,
semantic fingerprint `19560ff14e57ba251d1cb42c764bfb92f7cf77c27dfc358e056710563ebd6cdb`,
revision 14. Local/public-key independent и exact-image network-none проверки PASS.
Monitoring evidence и recurring pulses используют тот же прежний monitoring key.
На сервер переданы только публичные подписанные документы; закрытые PEM остались
во внешнем operator storage. Новая DPAPI config копирует прежние credentials;
это не новый пароль или ключ. Ограничения ACL/recovery ниже остаются незакрытыми.

Запись составлена: `2026-10-02 13:28:48 UTC`. Ответственный помощник:
`key_evidence_custodian`; решения об использовании принимает основной оркестратор,
независимую проверку границ секретов выполняет security reviewer.

Этот файл содержит только публичные идентификаторы и местонахождение. Это не
хранилище секретов и не доказательство наличия резервной копии ключа. Закрытые
значения, пароли, токены и содержимое окружения здесь запрещены.

## Существующие ключи подписи

| Назначение | Публичный Ed25519 ключ (hex, не SHA-256 fingerprint) | Внешнее расположение закрытого файла | Статус |
|---|---|---|---|
| Подпись release manifest | `65439f2f53e1a08e9dd15b80d07bbdb39c32f83ba482dbebe9c83311991e7772` | `D:\Projects\Yo_ai_manager\.local\time_tl_release_signing\ed25519-private.pem` | Найден существующий ключ; оркестратор сообщил `PUBLIC_KEY_MATCH` 2026-10-02 |
| Подпись monitoring evidence | `1a52a9d96acb88d89be3de992efb75d968620f642c1240dd1121b96ca74f320c` | `D:\Projects\Yo_ai_manager\.local\time_tl_monitoring_signing\ed25519-private.pem` | Найден существующий ключ; оркестратор сообщил `PUBLIC_KEY_MATCH` 2026-10-02 |

Публичные `.hex` файлы находятся рядом с соответствующими закрытыми файлами.
Соответствие публичным ключам, которым доверяет RC23, проверено оркестратором;
хранитель не открывал закрытые файлы и не воспроизводил криптографическую проверку.
Источник записи: переданный оркестратором контекст текущего выпуска; независимый
машинный receipt проверки пока не приложен. Точное время создания ключей не
установлено. Ключи **не создавались заново** в ходе регистрации 2026-10-02.

## Незавершённые проверки хранения

Обновление записи `2026-10-02 13:49:16 UTC`: оркестратор сообщил, что существующим
release key подписан RC23 manifest pending revision `13`; независимый signature
reviewer дал `PASS`. Закрытый PEM читался только signing operator для подписи,
не хранителем. Создания, ротации или копирования ключей не было. На сервер переданы
только публичный manifest и подпись. Процедура: [SIGNING.md](SIGNING.md);
идентификаторы и ограничения: [TIMELINE.md](TIMELINE.md).

- ACL: метаданные прочитаны оркестратором 2026-10-02, точное время операции не
  зафиксировано. Для обоих файлов текущий Windows оператор имеет `FullControl Allow`,
  наследование включено (`AreAccessRulesProtected=false`). Это не доказательство
  ограниченного ACL; полный effective-access review не завершён. ACL не изменялись.
- Шифрование носителя: `UNVERIFIED`.
- Отдельная защищённая резервная копия ключей: `UNVERIFIED`.
- Восстановление ключей из резервной копии: `UNVERIFIED`.
- Постоянный персональный владелец ключей: `UNASSIGNED`; помощник ведёт метаданные,
  но не заменяет владельца хранилища.

Наличие файла и совпадение публичного ключа не закрывают эти проверки. Не копировать
ключи в Git, архивы релиза или на сервер. Новое создание, перенос, ротация и удаление
ключей, а также смена серверного пароля требуют прямого запроса пользователя.
До санкционированного создания нового ключа определить защищённое расположение,
ACL, владельца и процедуру восстановления, затем внести публичные метаданные.

## Отдельно: шифрование резервной копии БД

Репетиционный backup RC23 защищён Windows DPAPI текущего операторского аккаунта.
Это не release/monitoring signing key и не переносимый парольный архив.
Восстановление требует того же Windows аккаунта на этой рабочей станции; независимое
восстановление при потере рабочей станции пока не доказано. См. [хронологию](TIMELINE.md).

Политика ведения: [POLICY.md](POLICY.md). После каждого изменения статуса приложить
санитизированный receipt с временем UTC; не заменять им приватный материал.

## RC26: публичная подпись кандидата — 2026-10-03 Киев

Существующий release Ed25519 anchor использован без создания/переноса/ротации ключа.
Private PEM остался во внешнем зарегистрированном месте; на сервер переданы только
публичные исходники, не ключ. Это candidate provenance, НЕ runtime attestation,
НЕ новое одобрение деплоя и НЕ новый source commit.

- Candidate attestation SHA256:
  `779c78f01268c3b9434d086b7d62eedb1f860fce5db794920fc31ba68bc942d2`.
- Detached base64/LF signature SHA256:
  `bcb52b72aa6c6b0bdbca24ae39c3ab721193205cd13758dd23b31bb6ce33b4a9`.
- Source archive SHA256:
  `ae0a715a6eeddd0e54bbcf45e3bb23b0ba2277e75087eb46a0f508a4a7da9e37`.
- Независимый C13-S3 public-only verifier: PASS; private key не читался.
- Evidence: `.release/time-tl-production-20261003-rc26-calendars/`.

Старые и новые TLBK1-копии проверены исходным существующим backup-derived ключом;
это не доказательство независимого восстановления самих ключей/их offhost escrow.
