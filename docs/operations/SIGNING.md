# Подпись выпуска существующим ключом

Использовать `.venv/Scripts/python.exe scripts/deployment/sign_release.py` с
`--manifest`, `--key` и `--signature`. Закрытый PEM остаётся во внешнем месте
из KEY_REGISTER.md; скрипт не создаёт ключи и не перезаписывает подписи.
До подписи подтвердить commit/tree/tag, SHA архива, registry digest, каталог,
целевую миграцию и фактическую следующую production revision.

Скрипт сверяет публичный ключ с compiled trust anchor, подписывает точные исходные
байты JSON Ed25519, кодирует detached signature в base64 с завершающим LF и
проверяет её штатным runtime verifier. Любое изменение JSON требует новой подписи.
Raw SHA-256 файла не равен semantic fingerprint runtime: записывать оба отдельно.
Публичные manifest/signature можно передать на сервер; закрытый ключ — никогда.
Каталог публичного mount должен быть доступен на traversal runtime UID, файлы
монтируются read-only. Проверить подпись в точном image с `--network none`;
это не production preflight. Независимый reviewer проверяет публичную подпись.

RC23: revision 13, raw manifest SHA-256
`38b2073df297d9b02b4564d02a56aabbb2dd43c1e7996196360f64bdd1383865`,
semantic fingerprint
`b169feed35bb7e8c25d54a6688127460877d94a2d0e86d080f8312ded5835ff8`.
Подпись создана 2026-10-02 существующим ключом, без ротации.
Production activation и end-to-end успех этим не подтверждаются.
