---
phase: 10
slug: admin-dashboard
status: draft
review_state: ready_for_check
shadcn_initialized: false
preset: none
created: 2026-09-20
---

# Phase 10 — Admin Dashboard UI Design Contract

> Owner-only monitoring and configuration panel for Time TL.

## Scope and safety boundary

The browser reads local cached state and metrics only; it never contacts `alerts.in.ua`, so dashboard traffic cannot increase provider load. Host metrics come from a dedicated read-only exporter. Never mount or query the Docker socket.

Never expose tokens, password material, environment values, raw coordinates, Telegram identifiers, usernames, lesson links, personal schedules, profile names, share tokens, or raw provider/Telegram payloads. User, mode, profile, and share information is aggregate only. Active alerts may show public provider regions/types, never subscribed users.

## Design System

| Property | Value |
|---|---|
| Tool | Manual CSS tokens and server-rendered components |
| Preset | Not applicable |
| Component library | None |
| Icons | Local pinned Lucide SVG subset; every status icon has text |
| Font | `system-ui, -apple-system, "Segoe UI", sans-serif` |
| Theme | System light/dark via `prefers-color-scheme` |

No React/Vite/shadcn system exists; do not add a registry. Primitives: `PageShell`, `StatusBadge`, `MetricCard`, `SectionCard`, `DataTable`, `InlineNotice`, `ConfirmDialog`, `Field`, `ProgressMeter`, `Sparkline`.

## Spacing Scale

| Token | Value | Usage |
|---|---:|---|
| `space-1` | 4px | Icon gaps |
| `space-2` | 8px | Inline controls/badges |
| `space-3` | 16px | Field/card spacing |
| `space-4` | 24px | Section/mobile gutter |
| `space-5` | 32px | Desktop gaps |
| `space-6` | 48px | Major breaks |
| `space-7` | 64px | Page separation |

Exceptions: controls/touch targets 44px; compact rows 48px; charts 120px.

## Typography

Exactly four sizes and two weights. Metrics use tabular numbers.

| Role | Size | Weight | Line height |
|---|---:|---:|---:|
| Label/metadata | 14px | 400 | 1.4 |
| Body/controls | 16px | 400 | 1.5 |
| Section heading/key metric | 20px | 600 | 1.3 |
| Page heading | 28px | 600 | 1.2 |

## Color

Status always uses icon + label + supporting text. Meet WCAG 2.2 AA.

| Role | Light | Dark | Usage |
|---|---|---|---|
| Dominant 60% | `#F8FAFC` | `#0B1220` | Page background |
| Secondary 30% | `#FFFFFF` | `#111827` | Cards/nav/dialogs |
| Accent 10% | `#2563EB` | `#60A5FA` | Primary CTA, focus, selected nav/range/series |
| Text | `#0F172A` | `#F8FAFC` | Primary text |
| Muted | `#475569` | `#CBD5E1` | Metadata |
| Border | `#CBD5E1` | `#334155` | Boundaries |
| Healthy | `#15803D` | `#4ADE80` | Healthy status |
| Warning | `#A16207` | `#FACC15` | Attention/near-stale |
| Critical | `#B91C1C` | `#F87171` | Stale/outage/destructive |
| Unknown | `#64748B` | `#94A3B8` | Unknown/unavailable |

Accent is reserved for save/sign-in, focused controls, selected navigation/range, and primary chart series; never health severity.

## Information architecture and layout

Navigation order: `Огляд`, `Тривоги та провайдер`, `Доставка`, `Користувачі`, `Сервер і воркери`, `Налаштування`, `Довідка`. Header always shows product, environment (`LOCAL`/`SIMULATION`/`STAGING`/`PRODUCTION`), aggregate state, updated time, owner menu, and `Вийти`.

Desktop: fixed 240px sidebar and 12-column content capped at 1440px. Below 1024px use top navigation; below 720px one column and tables become labeled cards. At 320px sparklines may become value + trend text. Never hide sections behind icon-only controls.

```text
┌──────── nav ────────┬──────────── header ────────────┐
│ sections            │ Time TL · PRODUCTION · Увага  │
├─────────────────────┼────────────────────────────────┤
│                     │ [safety/freshness banner]      │
│                     │ [4 aggregate metric cards]     │
│                     │ [provider 8] [rate budget 4]   │
│                     │ [resources 7] [workers 5]      │
└─────────────────────┴────────────────────────────────┘
```

## Owner authentication

No registration, email recovery, or roles. Bootstrap creates username `admin`. A cryptographically random one-time password is created only by an explicit admin CLI command in an interactive TTY, or delivered through a one-time file outside the container with mode `0600`. It must never be written to process `stdout`/`stderr`, application logs, Docker/Portainer logs, image layers, environment variables, UI, documentation, database plaintext, or this specification. Only its Argon2id hash is stored in the database. Plaintext is destroyed immediately after its first read, successful password change, or bootstrap TTL expiry, whichever occurs first. For Windows local bootstrap only, the one-time file may be gitignored and protected by an ACL granting access solely to the current user; production permits only interactive TTY display or an approved admin secret handoff, never a Portainer log or console transcript.

First login shows `Вход для владельца`, then forces `Создайте постоянный пароль` before dashboard content. The password must contain at least 14 characters, be entered twice, and be rejected when found in the configured breached/common-password denylist. Client-side checks assist only; the server repeats every validation. Exact validation messages are: `Пароль должен содержать не менее 14 символов.`, `Пароли не совпадают.`, and `Этот пароль слишком распространён или найден в утечках. Выберите другой пароль.` After success, invalidate the one-time credential and earlier sessions, destroy any remaining bootstrap plaintext, then show `Пароль изменён. Панель готова к работе.`

Prefill editable username `admin` for password managers. Generic error: `Не удалось войти. Проверьте данные или попробуйте позже.` The server stores failed-attempt counters in the database, keyed by account plus source IP, with bounded retention. Five failures within 15 minutes lock that account/IP key for 30 minutes. A successful login resets the account failure counter. During lockout show exactly: `Слишком много попыток входа. Повторите через {time}.` Do not disclose which credential failed. Client-side throttling is assistive only and never replaces server enforcement. Cookie: `Secure`, `HttpOnly`, `SameSite=Strict`; no auth data in `localStorage`. Session idle TTL is 30 minutes and absolute TTL is 12 hours; activity may extend only the idle deadline, never the absolute deadline.

`Выйти` ends the session. `Изменить пароль` requires current password, a new password satisfying the same 14-character and denylist rules, and confirmation; success invalidates other sessions. Idle or absolute expiry redirects with `Сеанс завершён. Войдите снова.` After re-authentication, the panel restores navigation context only and never automatically repeats a pending or previously submitted mutation.

## Page contracts

### Огляд

Banner precedence: provider `STALE/UNKNOWN`, delivery paused, critical worker missing, disk critical, degraded, healthy. Never use all-clear language for infrastructure.

Cards: total/active users; active alert episodes; outbox depth/oldest age; Telegram success/retry/failure; provider freshness; healthy/configured workers. Default 24h; ranges `1 год`, `24 год`, `7 днів`, `30 днів`, querying local aggregates only. Each card shows value, comparison, calculation time, detail link.

Empty: `Даних за цей період ще немає` / `Система почне показувати статистику після перших локально оброблених подій.`

### Тривоги та провайдер

Show explicit state (`Свіжі дані`, `Наближається поріг застарівання`, `STALE`, `UNKNOWN`, `Авторизацію відхилено`, `429 BACKOFF`); last validated success/age; last attempt/HTTP class; next attempt/backoff end; poll interval; stale threshold; hard per-minute ceiling; singleton lease; remaining token-bucket budget/reset; counts for `200`, `304`, `429`, `401/403`, `5xx`, timeout, invalid/partial response; latest snapshot; rejected all-clear transitions.

| State | Rule | Presentation |
|---|---|---|
| Fresh | age < 75% threshold | check + `Свіжі дані` + age |
| Near stale | age ≥75% and < threshold | warning + remaining time |
| `STALE` | age ≥ threshold/policy stale | critical + `Нові відбої заблоковано` |
| `UNKNOWN` | no validated state | unknown + `Стан не підтверджено` |

`304`: `Без змін; кеш підтверджено`. `429 BACKOFF` shows `Retry-After` and resume time. Dashboard refresh never triggers provider polling; no `Poll now`.

Active alerts list public region, hierarchy, type, provider start, duration, snapshot time. Empty: `У підтвердженому знімку немає активних тривог`. If stale/unknown add `Це не підтверджує відбій. Дані провайдера застаріли або недоступні.`

### Доставка

Outbox: pending, retrying, sent, permanent failure, oldest age, queue trend. Telegram: successful, retrying, `429`, blocked/forbidden, `5xx`, median/p95 latency, grouped by five-minute bucket/status. No recipient IDs or bodies.

When paused, every page shows `Надсилання призупинено з {time}. Події продовжують накопичуватися в черзі.` Include reason and `Відновити надсилання`; never discard queued work. Selecting resume must open a required `alertdialog` that shows the exact pending count and oldest queued-item age and warns: `После возобновления накопленные сообщения могут быть отправлены немедленно.` The primary/destructive CTA is `Возобновить отправку`; `Отмена` has initial focus. If the session expires, re-authentication returns to the paused state and does not reopen, submit, or repeat the resume action automatically.

### Користувачі

Aggregates only: total/new/onboarded, notifications enabled, pending deletions; lessons-only/lessons+sirens/sirens-only distribution; total/active/inactive profiles and users at three-profile cap; shares created/redeemed/expired/revoked, invalid imports, rate-limit violations; average profiles and percent with active profile. Merge buckets under 5 users into `Інше`; no drill-down.

### Сервер і воркери

Host cards: CPU, RAM used/total, application-data disk used/total, load 1/5/15, aggregate network receive/send, uptime. Each shows current value, 1h sparkline, sample time, threshold. No connections/IPs/packets.

| Metric | Attention | Critical |
|---|---|---|
| CPU | ≥80% for 5m | ≥95% for 5m |
| RAM | ≥80% | ≥90% |
| Disk | ≥80% | ≥90% |
| Load | ≥ CPU cores for 5m | ≥2× cores for 5m |
| Freshness | >2 intervals | >5 intervals/unavailable |

Missing metrics say `Метрики недоступні`, never `0%`. Footer: `Джерело: read-only host exporter`; hide endpoint/credentials. No Docker socket.

Rows for `bot`, `alert-worker`, `schedule-worker`, `sender-worker`, metrics collector: health, heartbeat/age, lag, process start, 24h restart count, safe error category. No commands/env. Missing heartbeat is `Немає heartbeat`, not `Stopped`, unless confirmed independently.

### Налаштування

Groups: `Провайдер`, `Нагадування`, `Режим обслуговування`, `Доставка`, `Секрети`, `Безпека власника`. Each has current value, validation, consequence, separate save.

| Setting | Control | Validation/behavior |
|---|---|---|
| Poll interval | Integer seconds | Minimum 15 client/server side; no catch-up burst |
| Stale threshold | Integer seconds | Minimum `2 × poll`; recommended 120; warn below 60 |
| Default reminder lead | Integer minutes | 0–120; defaults only, not existing overrides |
| Maintenance | Toggle + confirm | Polling/state/delivery continue; user config changes blocked |
| Pause delivery | Destructive toggle + confirm | Sender claims stop; polling/decisions/outbox continue; queue retained |

Provider save shows diff and uses `Застосувати налаштування провайдера`. Success: `Налаштування збережено. Наступний цикл застосує нові значення.` Failure: `Не вдалося зберегти налаштування. Значення не змінено.`

Maintenance confirmation says user changes stop while alert polling/delivery continue. Pause requires reason + acknowledgement and says new messages will queue while alerts continue. Cancel is initially focused. Resume always uses the required `alertdialog` contract above: pending count, oldest age, immediate-send warning, destructive/primary `Возобновить отправку`, initially focused `Отмена`, and no automatic action after re-authentication. Neither action deletes work.

Secrets show only name and `Налаштовано`/`Не налаштовано`; values, fragments and
lengths are never rendered. Following the owner's explicit 2026-09-23 requirement,
the authenticated PostgreSQL panel accepts write-only Telegram and alerts.in.ua
tokens in empty password fields. Blank keeps the stored ciphertext and explicit
clear removes it. The browser never contacts either provider directly. Database,
master-encryption, admin-IP-hash, SSH and TLS secrets remain infrastructure-only and
cannot be entered here. Saving creates a draft; requesting activation cannot itself
start workers or enable real delivery. Link to `SETUP_BOT.md` and `ADMIN_PANEL.md`.

Owner security includes password rotation, active-session count without IP/device, and confirmed `Завершити інші сеанси`.

### Довідка

Authenticated sanitized links: `Налаштування Telegram-бота` → `SETUP_BOT.md`; `Інструкція адміністратора` → `ADMIN_PANEL.md`. Never expose arbitrary repository files. Explain that `LOCAL`, `SIMULATION`, `STAGING`, `PRODUCTION` are separate evidence levels.

## Refresh and states

Fetch local summaries every 15s while visible and 60s in background; pause offline. `Оновити дані` refreshes only local API, disables while pending, preserves prior values. Each section shows relative + absolute `Europe/Kyiv` time.

On failure preserve data, mark `Можливо застаріло`, and say `Не вдалося оновити панель. Дані провайдера не змінювалися через цю дію.` UI backoff caps at 60s. Empty, unavailable, denied, partial, and stale are distinct; missing never becomes zero.

Required states: badges (healthy/attention/critical/unknown), cards (loading/value/empty/unavailable/stale/threshold), tables (loading/populated/empty/partial/failed), fields (focused/invalid/saving/saved/failed), dialogs (initial/blocked/submitting/failed), charts (loading/populated/insufficient/unavailable/stale), help links (available/unavailable with remediation).

## Interaction, accessibility, security

- Keyboard follows visual order; dialogs trap/restore focus; Escape never confirms.
- Destructive confirm is last in tab order and never initially focused.
- Mutations use CSRF, duplicate-submit prevention, redacted audit reference.
- Prompt on unsaved navigation; no optimistic safety-sensitive updates.
- Charts have text summaries; status never relies on color.
- WCAG 2.2 AA; semantic landmarks; one `h1`; 44×44px targets.
- `aria-live="polite"` without announcing unchanged polling; pause uses `role="alertdialog"`.
- Respect reduced motion; reflow at 320px and 200% zoom.
- Unauthenticated requests reveal no operational data.
- CSP blocks unapproved remote scripts/fonts; assets local/pinned.
- Browser never receives exporter credentials; no Docker socket or Portainer mutation.
- Audit owner, timestamp, action, result, redacted before/after values.

## Copywriting Contract

| Element | Copy |
|---|---|
| Dashboard CTA | `Оновити дані` |
| Settings CTA | `Застосувати налаштування провайдера` |
| Login CTA | `Увійти` |
| Generic error | `Не вдалося завантажити дані. Збережено останній успішний стан; повторіть спробу.` |
| Stale | `Дані провайдера застаріли. Нові відбої заблоковано до підтвердженого свіжого стану.` |
| Unknown | `Стан тривог не підтверджено. Це не означає відбій.` |
| Pause/resume | `Призупинити надсилання` / `Відновити надсилання` |
| Logout | `Вийти` |

Tone is calm and operational. Never use `Все добре`, `Безпечно`, or `Відбій` for infrastructure; reserve `відбій` for validated official transitions.

## Registry Safety

| Registry | Blocks | Safety Gate |
|---|---|---|
| shadcn official | none | Not applicable |
| Third-party | none | No third-party blocks permitted |

## Verification checklist

- [ ] Dashboard refresh cannot call alerts.in.ua.
- [ ] `200/304/429`, backoff, freshness, rate budget, singleton visible/labeled.
- [ ] `STALE/UNKNOWN` never appear healthy or all-clear.
- [ ] Alerts, outbox, delivery, aggregates, host load, heartbeats/restarts appear.
- [ ] CPU/RAM/disk/load/network/uptime become unavailable, never zero, on source loss.
- [ ] Poll interval below 15s is rejected client/server side.
- [ ] Maintenance and pause have distinct confirmed consequences.
- [ ] Secrets show presence/rotation guidance only.
- [ ] Bootstrap plaintext never reaches stdout/stderr or Portainer logs; approved TTY/file handoff, Argon2id-only storage, destruction, and TTL pass.
- [ ] Password length/denylist/confirmation, 5-per-15m lockout, 30m idle and 12h absolute TTL, logout, and secure cookie flows pass.
- [ ] Resume delivery alertdialog shows queue count/age and immediate-send warning; cancel is initially focused and re-auth never repeats the action.
- [ ] Responsive at 320/720/1024/1440px and 200% zoom; keyboard/screen reader pass.
- [ ] No prohibited sensitive field appears in rendered or API output.

## Checker Sign-Off

- [ ] Dimension 1 Copywriting: PASS
- [ ] Dimension 2 Visuals: PASS
- [ ] Dimension 3 Color: PASS
- [ ] Dimension 4 Typography: PASS
- [ ] Dimension 5 Spacing: PASS
- [ ] Dimension 6 Registry Safety: PASS

**Approval:** pending

## Decision sources

| Source | Decisions used |
|---|---|
| `AGENTS.md` | Safety, privacy, evidence, review boundaries |
| `.planning/PROJECT.md` | Mission, modes/profiles, fail-closed operation |
| `.planning/REQUIREMENTS.md` | Privacy, outbox, operations/sharing aggregates |
| `.planning/ARCHITECTURE.md` | Poll/rate/freshness/HTTP behavior, workers, observability |
| User task | Owner-only, load/stats, exporter, settings/secrets/auth |
| Defaults | Manual design system, responsive tokens, Ukrainian copy, accessibility |
