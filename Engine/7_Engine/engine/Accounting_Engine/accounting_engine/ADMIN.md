# Accounting Engine — administrator guide (multi-user service)

The same app runs in two modes:

| Mode | When | Who sees what |
|---|---|---|
| **Single-user** (laptop) | `AE_AUTH=off`, or `auto` with no account created yet | No login; everything under `data/` |
| **Multi-user service** | `AE_AUTH=on`, or `auto` once an administrator exists / `AE_ADMIN_EMAIL` is set | Login required. Each account has its own private folder `data/tenants/<id>/` (database, bill images, exports, uploaded templates). Users only ever see their own data; only the administrator sees the 🛡️ Administration page. |

Nothing in the bill-reading workflow changes between the modes.

---

## 1 · Deploy on your Ubuntu ARM64 server or a cloud VPS

### A. Docker + your domain (recommended — automatic HTTPS)

```bash
sudo apt install -y docker.io docker-compose-v2          # or Docker's own packages
unzip Accounting_Engine.zip && cd Accounting_Engine
cp .env.example .env && nano .env
```
Set at least:
```
AE_AUTH=on
AE_ADMIN_EMAIL=you@example.com          # first administrator, created on first start
AE_ADMIN_PASSWORD=a-strong-password
AE_SECRET_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
DOMAIN=bills.example.com                # DNS A/AAAA record → this server, ports 80+443 open
ACME_EMAIL=you@example.com
ANTHROPIC_API_KEY=sk-ant-...            # or paste it later in Administration → Integrations
```
```bash
mkdir -p data && sudo chown -R 10001:10001 data           # the container runs as an unprivileged user (uid 10001)
docker compose -f docker-compose.yml -f docker-compose.domain.yml up -d --build
docker compose logs -f                                    # first start prints the setup token if no admin was configured
```
Open `https://bills.example.com`. Caddy obtains and renews the Let's Encrypt certificate; the
app port is not exposed to the internet. Works identically on arm64 and amd64 (multi-arch images).

Update: `git pull`/unzip the new version over the folder, then the same `up -d --build` line.
`data/` is a bind mount and survives rebuilds.

### B. Docker without a domain (LAN / behind your own proxy)

`mkdir -p data && sudo chown -R 10001:10001 data`, then `docker compose up -d --build` → `http://SERVER-IP:8501`. Browsers block camera + microphone on
plain http from another device; put a TLS proxy in front or use option C with `AE_HTTPS=1`.

### C. Without Docker (systemd + Caddy from apt)

```bash
./run.sh                                  # creates .venv, installs packages, creates .env → edit it, run again
sudo cp deploy/accounting-engine.service /etc/systemd/system/   # edit User= / WorkingDirectory=
sudo systemctl daemon-reload && sudo systemctl enable --now accounting-engine
sudo apt install caddy
sudo cp deploy/Caddyfile.bare-metal /etc/caddy/Caddyfile        # edit the domain + e-mail
sudo systemctl reload caddy
```
Set `Environment=AE_ADDRESS=127.0.0.1` in the unit so only Caddy reaches the app.

### First administrator

* `AE_ADMIN_EMAIL` + `AE_ADMIN_PASSWORD` in `.env` → created automatically on first start, **or**
* leave them empty: the first visit shows *First-run setup*; the one-time token is printed in the
  server log (`docker compose logs` / `journalctl -u accounting-engine`) and kept in `data/setup_token.txt`
  until the administrator account has been created (the file is then deleted).

### Keys to keep safe

| | |
|---|---|
| `AE_SECRET_KEY` (or `data/master.key`) | encrypts the stored Claude / Razorpay keys. Losing it = re-enter those keys. |
| `data/master.db` | accounts, sessions, plans, subscriptions, payments, usage, settings, audit |
| `data/tenants/<id>/` | each user's private database, images, exports, templates |
| `.streamlit/secrets.toml` | Google sign-in client secret (optional) |

Back up `data/` as a whole (stop the container first, or use *Administration → Backups* for
consistent SQLite copies).

---

## 2 · Accounts

**Administration → Users**

* **Create user** – e-mail, name, business, optional start plan. A temporary password is shown
  once; the user must change it at first login.
* **Self sign-up** (*Create account* tab on the login page) lands as **pending**; approve or reject
  it in the *Pending* list. Approved users still need a plan before they can scan.
* **Google sign-in** – see §6. A Google account is linked to an existing user by e-mail only when
  Google reports the e-mail as verified and the account was created by you (never to an account
  somebody registered themselves with a password); a new e-mail is created as pending (or refused
  when sign-up is switched off in Customization).
* Manage a user: reset password (new temporary one), disable / re-activate (disabling revokes all
  their sessions immediately), change role (user ↔ admin; the last administrator cannot be
  demoted), allow / forbid their own Claude key, **delete** (also removes their data folder).
* Login protection: 6 wrong passwords for an account *from one connection* block that connection
  for 15 minutes (nobody can lock you out of your own account from elsewhere – the account only
  answers slowly after that many failures); 20 failed attempts (or 5 sign-ups) from one client
  address in 15 minutes are refused before they reach any account; the reply time is the same for
  unknown and known e-mails. Behind Caddy/nginx set `AE_TRUST_PROXY=1` so the real client address is
  used (the Docker domain stack does this). Sessions last 14 days (cookie); only a hash
  of the session token is stored; *Sign out* and a password change revoke sessions server-side, and
  disabling a user ends theirs immediately. Everything one browser tab remembered is wiped at sign-out
  and at sign-in, so two people sharing a computer never see each other's bills.

Account, plan, payment, key and customization actions are written to **Administration → Audit** (who, when, what, target; the newest 50,000 rows are kept).

## 3 · Plans, recharge, quota

**Administration → Plans & recharge** – plans have a name, days, scan quota (0 = unlimited),
price ₹ and a description. Defaults: Trial 7 days/25 scans (free), Monthly 30 d/500 scans ₹999,
Quarterly, Yearly. Edit or add your own; deactivate old ones.

How access works:

* A user needs an **active period** (a subscription whose end date is not past). Without one the
  app shows *Subscription required* with the recharge options; the user's data is kept.
* Every **scan** (bill photo, dictated/typed bill, bank slip) takes one unit of the quota *before*
  the reader runs (atomically – two simultaneous scans cannot both use the last unit); a scan that
  fails (network error, key rejected) gives the unit back. When the quota is used up the reader
  refuses new scans with a clear message, but the app stays usable to finish, save and export what
  was already read.
* Scans made with a user's **own Claude key** are metered but not counted against the quota.
* **Recharge stacking**: a new plan starts *after* the current usable period ends; if the current
  quota is already exhausted the new plan starts today.
* Administrators are never gated.

Recharge paths:

1. **Manual** – the user sees your UPI ID / support contact (set in Customization) and pays;
   you add the plan under *Users → manage → Add plan*.
2. **Razorpay** – paste key id + secret in *Integrations*; users get a *Create payment link* button
   (*My Account → Subscription → Pay online*) that creates a Razorpay Payment Link, pay on Razorpay's page, then click *Check payment* (or you
   click *Check pending payments* under *Payments*). A paid link activates the plan once, however
   often it is checked; the amount and currency reported by Razorpay are verified against the plan
   (a mismatch is marked *failed* and written to the audit log). Test with `rzp_test_…` keys first.

**Usage** shows scans, tokens and estimated cost per user (prices per model editable in
*Integrations → prices*).

## 4 · Customization (global vs per user)

**Administration → Customization** sets the defaults for everyone; the *Customization for this user only* form under **Users → manage**
overrides any field for one user only (that user alone sees it).

| Field | Effect |
|---|---|
| `app_name`, `logo_url`, `primary_color`, `custom_css` | branding (only the administrator can set these; the CSS is injected as-is apart from a basic script filter) |
| `welcome_text` | text on top of New Bill |
| `announcement` | banner on every page (blank = none) |
| `labels_json` | rename pages, e.g. `{"📄 New Bill": "Naya Bill"}` |
| `features_json` | switch features: `voice`, `bank_slips`, `tally_import`, `own_api_key`, `google_login`, `signup` |
| `prompt_notes` | extra instructions to Claude (trade conventions, item names) – global + per user are combined |
| `default_tally_json` | Tally voucher settings applied to **new** users' folders (sales ledger, voucher types…) |
| `support_contact`, `upi_id` | shown on the subscription / recharge page |

Users cannot change any of this; their own *Settings* page only holds their Tally names, bank
accounts, templates and backups.

## 5 · Claude key & costs

*Integrations → Claude* – the master key is stored encrypted and used for every user by default
(`ANTHROPIC_API_KEY` in `.env` is the fallback). A user may add their own key under *My Account →
Claude key* when allowed (globally via `features_json.own_api_key`, and per user).
Scans run concurrently per user – each request carries its own key and is metered separately.

## 6 · Google sign-in (optional)

1. Google Cloud console → *APIs & Services → Credentials → Create OAuth client ID (Web)*.
   Authorised redirect URI: `https://YOUR-DOMAIN/oauth2callback`.
2. `cp .streamlit/secrets.toml.example .streamlit/secrets.toml`, fill in `client_id`,
   `client_secret`, `redirect_uri`, and a random `cookie_secret`. Restart the app.
3. *Integrations* shows *Google sign-in: configured*; the login page gets **Continue with Google**.
   Switch it off for everyone with `features_json: {"google_login": false}`.

Google sign-in needs HTTPS and the domain in the redirect URI (option A or C above).

## 7 · Backups & restore

*Administration → Backups* downloads the master database (SQL dump) and any user's `accounting.db`.
For a full restore stop the app and copy `data/` back. Users can back up / restore their own
database from *Settings* as before.
