# Accounting Engine — development roadmap & decision log

## Decisions taken (26 Sep 2026)

| Question | Decision |
|---|---|
| AI backend for handwriting + dictation | Anthropic Claude API (vision + text, forced tool-use JSON). Speech-to-text: browser mic → Google free web-speech via `SpeechRecognition` (no key). |
| Output | Sheet **Ledger** = exact Tally "Ledgers in Default Template" (party block, continuation rows) + sheet **Items** with line items. |
| Missing bill date / number | Never guessed – the review screen asks (date defaults to today, *Auto-number* available). Printed page numbers on the bill book are ignored. |
| Platform | Local Streamlit web app on the Windows laptop, SQLite database, usable from phones on the same Wi-Fi. **Also runs on Ubuntu Linux x86_64 / aarch64** (`run.sh`, systemd unit, multi-arch Dockerfile); optional self-signed HTTPS so phone camera/mic work over the LAN. |
| Default state / country / group | Assam / India / Sundry Debtors (sale) – Sundry Creditors (purchase). Editable in `.env`. |
| Amount to book | Sum of line amounts (written or computed) by default; user can choose the written total when they differ. |

## Phases (all complete)

| # | Phase | Verification |
|---|---|---|
| 1 | Scaffold, pydantic models, SQLite schema, Tally template importer | 4 tests: upsert/alias, bill round-trip, items master, import of template (15 ledgers, 4 bills, idempotent) |
| 2 | Party/item fuzzy matcher, arithmetic validator | tests on 5 sample parties, spelling variants, false-positive guards, struck-out lines, mismatch warnings |
| 3 | Claude extractor (image, PDF, text) with offline mock mode | tests with fake SDK client (model fallback, truncation), image downsizing/EXIF/media types, messy model output |
| 4 | Tally Excel exporter | layout compared cell-by-cell with the template; export → import round trip |
| 5 | Streamlit UI (5 pages) | Playwright end-to-end: import → read sample bill → new party → save → auto-match → auto-number → export → masters |
| 6 | QA audit agent + independent re-audit agent | 17 findings fixed (data-editor edit loss, matcher false positives, upload queue, image/media handling, stereo WAV, backup/restore safety, run.bat…); 45 tests green |
| 7 | Packaging: README, run.bat/run.sh, .env.example, delivery | this document |
| 9 | Tally master import (List of Ledgers PDF/xlsx/csv/txt with learned custom groups; Stock Summary xlsx with units from number formats), party-first check and **item check with explicit confirmation** before new items are created; `pypdf` layout-mode extraction | 66 tests incl. real 7,879-ledger / 715-item exports; Playwright e2e (`probe_tally.py`); QA audit (12 fixes) + independent re-audit (numbered-unit party rule, initials in item names, godown heuristic, 3 perf fixes) |
| 10 | **Tally voucher export** in every format Tally imports: voucher XML (native), masters XML, voucher Excel (one row per entry, mapped once in Tally), CSV; per-bill download right after saving; Tally settings (ledgers, voucher types, company, godown, inventory / accounting-only, units) | 78 tests: XML parsed, sign conventions and Σ=0 for sale/purchase × invoice/voucher mode, masters, xlsx/csv rows, bundles; Playwright downloads; QA audit (9 fixes: ALLLEDGERENTRIES in voucher mode, unit mismatch warnings, rounding/escaping) + re-audit (balancing-line sign flag, unit fallbacks, stale quick-export cache) |
| 11 | **Batch reading** (many photos at once → summary table → one review card per bill → *Save all ready*), **Bank Slips** page (deposit counterfoils → Receipt / Contra vouchers with bank allocations, learned bank-account → ledger mapping, words-vs-figures check), **Tally official Excel templates** (Accounting Voucher / All Accounting Masters bundled; user-uploaded template followed exactly) | 105 tests; Playwright e2e over batch bills, 3 slips, exports; QA audit (14 fixes: radio label state loss, card state loss on 'Read more' / page switch, custom-template column alignment, Receipt bill allocations, export-on-every-rerun) + re-audit (instrument bank in bank allocations, words≠figures gating, mark-exported state, camera file names) |
| 12 | **Bill total under the items** (items total vs written total, ✅/⚠️), **one tab per bill / per slip** on the review screen (save one or *Save all ready*), Bank Slips default *Receipt* | Playwright e2e over tabs; 114 tests |
| 13 | **Multi-user service**: accounts (admin-created, self sign-up + approval, Google OIDC via `st.login`), PBKDF2 passwords, lockout, 14-day cookie sessions, **private tenant folder per user** (`engine/tenant.py` ContextVar → separate SQLite/images/exports/templates), `data/master.db` (users, sessions, plans, subscriptions, payments, usage, settings, audit) | `tests/test_tenant.py`, `tests/test_master.py`; multi-user Playwright e2e (`mu_smoke.py`: admin creates user → forced password change → scan → quota → isolation → disable revokes session → sign-up approval) |
| 14 | **Subscriptions & metering**: plans (days + scan quota + price), manual recharge by the administrator, Razorpay payment links, stacking rule, quota gate inside the extractor (`engine/runtime.py`: per-request Claude key – master key by default, user's own key optional and encrypted – usage/cost per scan) | e2e: 500→499 scans, 1-scan plan refuses the second scan without counting it, no-plan user sees *Subscription required* |
| 15 | **Administrator panel** (Users · Plans & recharge · Payments · Usage · Customization · Integrations · Audit · Backups); global customisation (branding, CSS, labels, feature flags, prompt notes, default Tally settings, support/UPI) with **per-user overrides visible only to that user**; users cannot change UI/backend | e2e: app name + announcement seen by user, per-user welcome text not seen by admin |
| 16 | **Deployment for the ARM64 server / VPS with a domain**: `docker-compose.domain.yml` + Caddy (automatic Let's Encrypt), `.env.example` (AE_AUTH, admin bootstrap, AE_SECRET_KEY, cookie flag, DOMAIN), `.streamlit/secrets.toml.example` (Google), bare-metal Caddyfile + systemd, `ADMIN.md` | `docker compose config` validated both stacks; `cryptography`/`Authlib` aarch64 wheels verified |
| 17 | **Security QA audit + independent re-audit** of the multi-user layer. Fixed: widget callbacks ran without a tenant (strict tenant resolver – no silent fall-back to a shared DB), browser-tab state not cleared between users, restored backups could point image paths outside the user's folder, quota check-then-act race (atomic reservation, released on failure), Razorpay confirmation not idempotent / amount unverified / renamed-plan crash, account lockout usable as a denial-of-service (per-client lock + slow-down + per-IP throttle), Google linking of self-registered e-mails (verified e-mail + sign-up source), login-timing enumeration, hashed session tokens with UTC expiry, Markdown/HTML injection in the admin panel, audit growth, non-root container, HSTS, one-time setup token, proxy-trust flag | 121 tests (incl. 8-thread quota race, 4-way payment confirmation race, migration of an old master.db); mu_smoke + ui_smoke green; re-audit confirmed every item FIXED |
| 8 | Linux / ARM64 support: hardened `run.sh` (venv, headless, LAN address, port, `--check`, self-signed HTTPS), `Dockerfile` + `docker-compose.yml` (amd64/arm64), `deploy/accounting-engine.service` | aarch64 wheel availability verified with `pip download --platform manylinux_2_28_aarch64` for py3.10/3.12; `run.sh` exercised end-to-end on Linux incl. HTTPS + Playwright page check |

## Architecture

```
app.py                 single-user vs service mode, gate, tenant + runtime activation, navigation
ui/new_bill.py         capture → extract → review → save
ui/bills.py            browse, export, delete
ui/masters.py          parties & items
ui/settings.py         stats, Tally import, backup/restore
ui/common.py           items table <-> pydantic helpers
engine/config.py       .env driven settings
engine/models.py       LineItem, BillDraft, Party, Bill, ValidationReport
engine/db.py           SQLite (parties, bills, bill_items, items, settings), WAL, per-call connections
engine/importer.py     Tally template → DB
engine/matcher.py      rapidfuzz party/item matching: noise/type words, initials, digit-token rule (never auto-match across numbers)
engine/tally_import.py Tally 'List of Ledgers' (PDF/xlsx/csv/txt) & 'Stock Summary' (xlsx/csv) parsers + apply to DB
engine/tally_export.py Voucher XML / masters XML / voucher xlsx (row layout + Tally official template) / csv, simple vouchers (Receipt/Contra), TallySettings, bundles
engine/bank.py         Bank deposit-slip extraction, bank_txns table, account→ledger mapping, Receipt/Contra vouchers
engine/templates.py    Official vs user-uploaded Excel templates (voucher / masters), validation
ui/bank_slips.py       Bank Slips page (batch read → cards → save all → export)
engine/validator.py    fill missing amounts, arithmetic checks, missing-field list
engine/extractor.py    Claude vision/text → BillDraft (tool schema, prompt with trade conventions, mock mode)
engine/exporter.py     Tally workbook (starts from the template file to keep styling) + Items sheet
engine/service.py      use-cases: resolve party, commit bill (party + alias + image + bill)
engine/speech.py       WAV → text
engine/tenant.py       per-user data folder (ContextVar); legacy single-user layout when none is active
engine/master.py       master.db: users, sessions, plans, subscriptions, payments, usage, settings (global / per user), audit
engine/runtime.py      per-request runtime: Claude key (master or user's own), prompt notes, quota gate, usage metering
engine/secrets.py      PBKDF2 password hashing, Fernet encryption of stored keys, tokens
engine/payments.py     Razorpay payment links (create / check / activate plan)
ui/auth_ui.py          login, sign-up, first-run setup, Google return, cookie sessions
ui/account.py          My Account: subscription & recharge, password, own Claude key; blocked page
ui/admin.py            Administration panel
ui/theme.py            branding / CSS / labels from the customization
```

## Next steps (not built yet – pick what you need)

1. **Live-API acceptance run** on the 5 real photos with your key; tune `SYSTEM_PROMPT` wording
   for any systematic misreads (e.g. variety abbreviations).
2. ~~Voucher export~~ — done (phase 10). Next: verify one import on your Tally company and adjust
   ledger/voucher-type names in Settings; optionally GST ledgers per line.
3. **GST fields** (GSTIN, tax ledgers) on parties and bills if you move from kachha to pakka bills.
4. ~~Multi-user / hosted deployment with a login~~ — done (phases 13–16). Next: e-mail sending
   (password reset links, sign-up notifications) via SMTP; Razorpay webhook instead of *Check payment*.
5. **Outstanding & receipts**: record payments against bills and export receipt vouchers.
6. **Packaging as .exe** (PyInstaller) for machines without Python.
7. ~~Proper TLS certificate~~ — done: Caddy + Let's Encrypt with `docker-compose.domain.yml` / `deploy/Caddyfile.bare-metal`.

## Known limits

* Multi-user: the Razorpay flow (payment link → *Check payment*) and Google sign-in follow the documented APIs
  but could not be exercised against the live services here; test with `rzp_test_` keys / your OAuth client first.
* Passwords are reset by the administrator (no e-mail sending yet).
* Streamlit keeps one Python process; hundreds of simultaneous users would need more than one container
  behind Caddy (each user's data is file-based, so scaling out is a matter of shared storage).

* Bank slips: the depositor is not written on a PNB counterfoil, so a Receipt needs you to pick the party; words-vs-figures disagreements block saving until confirmed.
* Tally's Excel import of `Bank Allocations - Transaction Type = Cash` and of *On Account* bill allocations without a name could not be verified without a live Tally; the XML follows Tally's own export shapes.

* The voucher XML follows Tally's own export shapes but could not be imported into a live Tally here; the first
  real import will tell if a ledger name, voucher type or unit differs from your company (Tally reports the exact line).
* Tally does not import CSV directly (save as .xlsx first); the voucher Excel needs a one-time column mapping in Tally.

* Ledger-list parsing separates *Name* from *Under* by text alone; a custom group whose name ends in a default group name (e.g. "HDFC Bank Accounts") is split wrongly – check the preview's *Examples* column and correct the group on the Parties page.
* Stock Summary godown rows are detected by repetition; a genuine item directly following another with identical qty/rate/value is listed under *Skipped lines* in the preview.

* Handwriting accuracy depends on the photo – flat, well-lit, whole bill in frame.
* Voice recognition uses Google's free endpoint (needs internet); Assamese/Bengali quality varies.
* `Auto-number` consumes a sequence number even if the bill is then discarded (gaps possible).
* Keep `data/` on a local disk (not OneDrive-synced) – SQLite WAL and sync clients don't mix.
