# Electricity schedule sources — 2026-11-04

Subscribed API update: admin key entry/check deployed; owner record VALID at
17:02 Kyiv. Four bounded authenticated reads verified the Dnipro supplier registry
and 12 groups each for DTEK/CEK. No automatic polling, persisted graph or messages.
Publication age and last source check are distinct; LOCAL normalization now keeps
both timestamps and preserves emergency/no-schedule suppression. Address API and
Telegram wizard are reviewed LOCAL only. See ENERGY-API-ADMIN-20261104.md.

This register records the initial public-page/source audit. The user later
confirmed subscribed Alerts Energy API access; README/WORK-STATE supersede the
HTML-only permission/selection conclusions for that documented API route.

Task: POWER-G0-SOURCES. Evidence: a bounded read-only audit of official public
pages; no live poller, production change, address lookup, WAF bypass, browser
impersonation or third-party schedule feed. These findings do not prove the
current state of electricity supply. Source gates are independent of alerts.in.ua.

## Gate summary

| Provider | Official page | Observed access | Runtime/source gate |
| --- | --- | --- | --- |
| DTEK Dnipro | https://www.dtek-dnem.com.ua/ua/shutdowns | Ordinary HTTP GET returned HTML; web text extraction exposed only an iframe | **BLOCKED**: wildcard robots prohibition; no verified data/status contract |
| CEK | https://cek.dp.ua/index.php/cpojivaham/vidkliuchennia.html | Public outage article and linked schedule form accessible | **BLOCKED**: conflicting/insufficient dated schedule evidence; automation terms/rate unconfirmed |

All official DTEK/CEK adapters described here must remain `UNAVAILABLE` without
network polling until their individual gates are accepted. An independent fake provider may exercise
offline functionality, but is not a substitute for either official integration.

## DTEK Dnipro

- Official root: https://www.dtek-dnem.com.ua/ . Schedule page above.
- Access-policy evidence: https://www.dtek-dnem.com.ua/robots.txt returned HTTP200.
  Its wildcard group is `User-agent: *` / `Disallow: /`. Separate named search-bot
  rules do not grant this application access. No further DTEK requests followed
  the audit decision; do not impersonate those bots.
- The observed HTML declares an AJAX base `/ua/ajax`. This is **not** a verified
  schedule API: no AJAX request or JSON response was made/accepted.
- `getStreets`, `getHomeNum`, `preset`, `fact`, `GPV` identifiers and meanings of
  `YES/NO/MAYBE/FIRST/SECOND/MFIRST/MSECOND` remain unverified against an official
  frontend/data pair. No partial-hour or complement-state conversion is approved.
- No daily/weekly completeness, source timestamp, calendar dates, all-group
  response or source timezone was verified. Internal presentation will use
  `Europe/Kyiv`; that is an application contract, not a verified source fact.
- No application automation terms, authorization or applicable rate limit was
  established. The named-bot crawl settings are not this application's limits.
- The supplied https://github.com/shed-crypto/dtek-blackout-client is orientation
  only, never authority, a dependency, a fallback or permission to bypass WAF.
- Fixture: `tests/fixtures/power_sources/dtek-robots-wildcard.observed.txt` is
  only the exact restrictive wildcard excerpt, not the complete robots file or
  a schedule response. No CSRF tokens, cookies or addresses are retained.

Required before enabling: operator-permitted automated access, an accepted exact
endpoint and parameters, official example responses, independently established
status/mapping/date/timezone/completeness semantics and an approved poll budget.

### Additional DTEK regions from the user's extension

The five region codes are explicit identities; an unknown code never falls back
to Kyiv or substitutes another region's schedule. In a few ordinary GETs on
2026-11-04, only robots policy resources were requested for the added origins:

| Code | Configured official schedule URL | Observed robots result / source gate |
| --- | --- | --- |
| `dn` | https://www.dtek-dnem.com.ua/ua/shutdowns | Wildcard disallow, as above; not re-requested |
| `k` | https://www.dtek-kem.com.ua/ua/shutdowns | HTTP200 challenge HTML instead of robots policy; stop, no challenge resource fetched |
| `kr` | https://www.dtek-krem.com.ua/ua/shutdowns | HTTP200 challenge HTML instead of robots policy; stop, no challenge resource fetched |
| `o` | https://www.dtek-oem.com.ua/ua/shutdowns | HTTP200 robots wildcard `Disallow: /`; stop |
| `d` | https://www.dtek-dem.com.ua/ua/shutdowns | HTTP200 robots wildcard `Disallow: /`; stop |

For these four added origins, shutdown-page payload availability was not proved:
the access gate was already closed. A configured URL or HTTP200 challenge is not
evidence that dated schedule data is accessible. No browser/Playwright bypass,
proxy, impersonation, retries or other-region fallback was used.

### Technical-reference extraction implementation

The requested mr-devboy reference's
[schedule.js](https://github.com/mr-devboy/dtek-schedule/blob/main/src/schedule.js),
[constants.js](https://github.com/mr-devboy/dtek-schedule/blob/main/src/constants.js)
and [helpers.js](https://github.com/mr-devboy/dtek-schedule/blob/main/src/helpers.js)
were read as third-party format orientation, not run or copied as a service.
The reference loads browser HTML, extracts a fact literal, uses one selected
group and merges adjacent equal states. Its extraction expression, retries,
global state, Telegram sender and default-region fallback are not reused.

The Python adapter has a strict static script-only literal extractor (no JS
execution), duplicate/depth/size/completeness validation and a versioned
`reference-v1-synthetic` profile. It handles all twelve groups in one response.
Unknown groups cannot be silently discarded; a future extra group requires a
separately reviewed mapping version and compatible internal domain support.
Unknown state tokens remain UNKNOWN, never inferred `yes`. The four technical
yes/no/first/second half-hour meanings are tested only on explicitly SYNTHETIC
fixtures, not presented as verified official state semantics.

Twenty-four civil-hour keys cannot express an offset/fold during a 23/25-hour
Kyiv DST day: this technical profile refuses that day rather than inventing a
missing or repeated hour. Source timestamps require explicit epoch/offset or the
profile's explicit year-bearing Kyiv format; invalid/future times fail closed.
Source/fetch/schedule dates remain separate, stale data does not become FRESH.

`DtekHtmlProvider` can run the whole pipeline with an explicitly injected fake
HTTP/browser adapter in local/test/simulation SYNTHETIC mode. Normal runtime
has no source approval flag: its immutable source gate refuses before transport
construction/use. The ordinary HTTP transport independently checks the gate;
it does not follow redirects, use proxy environment or auto-fallback to a browser.
Playwright is not installed or required: no live browser process is started.
The pure local `inspect_html_fact` diagnostic returns only counts/date/update
metadata and explicitly false readiness/semantic-verification fields.

## CEK

- Official root https://cek.dp.ua/ links the outage article in the summary.
  That article links this actual schedule page:
  https://cek.dp.ua/index.php/cpojivaham/vidkliuchennia/2-uncategorised/921-grafik-pogodinikh-vidklyuchen.html .
- Observed interface: ordinary HTML form, `method="post"`, same-page action,
  fields `queue` and `date`; `queue=all` and literal options `1.1` through `6.2`.
  No XHR/fetch JSON endpoint was identified. No hidden API is inferred from this.
- One read-only selector submission used `queue=all`, `date=2026-11-04`.
  HTTP200 response contained twelve empty `timesN` arrays and no record tables.
  Its no-records label is retained in the small observed HTML fixture.
- The outage article simultaneously displayed twelve groups for November4 with
  nonempty intervals, but without a verified year/source-update timestamp.
  Thus the form's empty response is **not** evidence of all-day expected power,
  nor can its reflected input date be treated as the publication's date.
- Observed `Last-Modified` values tracked request times on the dynamic pages;
  their reliability as schedule-publication timestamps was not established.
  Joomla article creation dates and the footer are not daily schedule dates.
- The article states next-day schedules are published after operator orders;
  a weekly forecast or available future date range was not established.
- https://cek.dp.ua/robots.txt allows no affirmative API license: no wildcard
  prohibition was observed for these public article paths, while internal
  `/components/`, `/modules/`, `/plugins/` and other application paths are denied.
  None of those denied paths was fetched. Robots absence is not operator consent.
- No published automation terms or applicable rate limit were verified.
- Literal form groups are observed identifiers only; actual dated group-record
  equivalence, interval semantics, publication coverage and source timezone still
  require evidence. Do not infer provider/group from an alert territory.
- Fixture `cek-no-records.observed.html` is a normalized extract of selected public
  date/no-records fragments and empty arrays, not a byte-exact whole response or
  an accepted schedule. Its
  accompanying JSON records the request and limits. It contains no personal data.

Required before enabling: resolve the publication/date discrepancy, obtain a
dated nonempty official response plus no-schedule/partial response fixtures,
verify all-group mapping and interval meaning, establish permissible access and
rate, and independently compare accepted normalization to the official display.

## Shared freshness and runtime rules

Application defaults of poll300s / stale1800s are proposals, not provider limits.
Source approval must specify a provider-safe minimum; until then no runtime HTTP
requests or notifications are permitted. Observe Retry-After, backoff and circuit
breaking only after a source is admitted. Fetch time cannot invent source time,
publication year, complete coverage or an electricity-restoration confirmation.
Empty/partial/conflicting/error/stale data never mean `POWER_EXPECTED`.
`NO_SCHEDULE` requires explicit unambiguous publication evidence for that date;
this CEK observation does not satisfy it. A forecast never becomes confirmed day
data. The configured TTL cannot make an unverified source `FRESH`.

## Evidence limits

Initial audit methods were a few ordinary GETs and one demonstrated CEK selector POST,
not a load test, interval probe or production preflight. No actual positive
schedule JSON fixture was obtained. No third-party package/code was installed
or executed, and no migration, Telegram sender, database, application server or
deployment was executed. The initial audit wrote only the register/observed
extracts; the subsequent explicitly offline implementation imported its own
Python code for LOCAL tests. Current focused result:89 tests passed, strict mypy
passed for eight provider modules, Ruff passed for those modules/two test files.
That LOCAL evidence does not change any official source gate above.
