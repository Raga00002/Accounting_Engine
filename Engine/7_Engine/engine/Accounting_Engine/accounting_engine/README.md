# Accounting Engine — bill / bank-slip photos & voice → Tally

A web app for **Wings Industries** (and, in service mode, for any number of separate businesses):
photograph (or dictate) handwritten kachha bills and bank deposit slips, let Claude read them,
confirm the party / items and any missing details, and get every file Tally Prime imports
(voucher XML, masters XML, the official *Accounting Voucher* Excel template, row-layout Excel, CSV,
ledger master Excel) — plus a party/item database that learns as you go.

It runs **single-user on a laptop** (no login) or as a **multi-user service** on your Ubuntu
ARM64 server / a cloud VPS with a domain: accounts, subscriptions (manual recharge or Razorpay),
private data per user, an administrator panel for customisation — see **`ADMIN.md`**.

```
photo / voice / typed text
        │
        ▼
 Claude extraction  ──►  party fuzzy-match against DB  ──►  review screen
 (items, qty, weight,     (auto / confirm / add new,         (fix anything, add date &
  rate, amount, total)     alias remembered)                  bill no., arithmetic checks)
        │                                                             │
        ▼                                                             ▼
   SQLite database  (parties · bills · line items · items master)   Save
        │
        ▼
 Excel export: sheet "Ledger" (exact Tally template) + sheet "Items" (line items)
```

## 1 · Install (Windows)

1. Install **Python 3.11 or newer** from python.org (tick *"Add python.exe to PATH"*).
2. Unzip / copy this folder anywhere, e.g. `Documents\Accounting_Engine`.
3. Double-click **`run.bat`**. The first run creates a virtual environment, installs the
   packages and opens `.env` in Notepad.
4. Paste your Anthropic API key into `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   AE_EXTRACTOR_MODE=live
   ```
   (keys: <https://console.anthropic.com>). Save, close Notepad, double-click `run.bat` again.
5. The app opens in your browser at <http://localhost:8501>. Phones on the same Wi-Fi can use
   the *Network URL* printed in the black window (camera + mic need Chrome/Edge; on a phone
   allow the browser to use the camera).

## 1a · Multi-user service (accounts, subscriptions, private data per user)

Set `AE_AUTH=on` plus `AE_ADMIN_EMAIL` / `AE_ADMIN_PASSWORD` in `.env` (or use the one-time setup token
printed in the log). Users sign in (password or Google), each gets a private folder under `data/tenants/`,
scans are metered against their plan, and the administrator manages users, plans, recharges, branding,
feature flags and Claude keys from **🛡️ Administration**. Everything is described in **`ADMIN.md`**.

## 1b · Install on Ubuntu Linux (x86_64 **or aarch64 / ARM64** – Raspberry Pi, ARM servers, Ampere, Apple-silicon VMs)

Every dependency ships prebuilt `aarch64` wheels (verified for Python 3.10 and 3.12), so nothing is compiled.

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip openssl   # flac only for .flac uploads
unzip Accounting_Engine.zip && cd Accounting_Engine        # or: cd ~/Documents/Accounting_Engine
chmod +x run.sh
./run.sh            # 1st run: creates .venv, installs packages, creates .env and stops
nano .env           # paste ANTHROPIC_API_KEY=sk-ant-...   (keep AE_EXTRACTOR_MODE=live)
./run.sh            # starts the server
```

`run.sh` prints two addresses:

| Open from | URL | Camera / mic |
|---|---|---|
| a browser on the Ubuntu machine itself | `http://localhost:8501` | works |
| a phone/laptop on the same network | `http://<ubuntu-ip>:8501` | **blocked by the browser** on plain http |
| a phone/laptop, started with `AE_HTTPS=1 ./run.sh` | `https://<ubuntu-ip>:8501` | works (accept the self-signed certificate once) |

Useful variants: `AE_PORT=8600 ./run.sh` (other port) · `./run.sh --check` (verify install only) ·
`AE_ADDRESS=127.0.0.1 ./run.sh` (local only). The Excel files are also written to `data/exports/`
on the Ubuntu machine, so on a headless box you can `scp` them if you prefer.

**Run at boot (systemd):** `deploy/accounting-engine.service` – edit `User=`/`WorkingDirectory=`, then
`sudo cp deploy/accounting-engine.service /etc/systemd/system/ && sudo systemctl enable --now accounting-engine`.

**Docker (multi-arch, amd64 + arm64):** `docker compose up -d --build` (uses `.env`, keeps `data/` on the host).
**With a domain + automatic HTTPS (Caddy / Let's Encrypt):** set `DOMAIN` and `ACME_EMAIL` in `.env`, then
`docker compose -f docker-compose.yml -f docker-compose.domain.yml up -d --build` — details and the
multi-user setup (accounts, plans, Google sign-in) in `ADMIN.md`. The compose files were validated with
`docker compose config`; the image was not built in this environment – it follows the standard `python:3.12-slim` pattern.

Mac: `./run.sh` works the same way.

## 2 · First-time setup inside the app — load your Tally masters

**⚙️ Settings & Import → Import parties & items from Tally.** Export from Tally Prime and upload the file;
the app detects the report type, shows a preview, and you confirm before anything is written.

| Tally export | Path in Tally | Formats accepted | What is imported |
|---|---|---|---|
| **List of Ledgers** (`Master.pdf`) | Gateway → Display More Reports → List of Accounts → Ledgers → Export (or Alt+E) | PDF, Excel (.xlsx), CSV, or the PDF's text (.txt) | Ledger name, group (*Under*), opening balance Dr/Cr. Names that wrap onto two lines, serial numbers with commas and user-defined groups (e.g. *GEM*, *Govt. Debtors*, sub-groups) are handled. You tick which groups are parties — debtor/creditor-type groups are pre-ticked, expense/bank/tax ledgers are not. |
| **Stock Summary** (`StkSum.xlsx`) | Gateway → Stock Summary → Export | Excel (.xlsx), CSV | Item name, stock group, unit (read from Tally's number format, e.g. *Pcs*), last rate. Group rows, godown rows and *Grand Total* are skipped. |
| **Ledgers in Default Template** | Import-format workbook | Excel | Parties with address/state/pincode and open bills (marked *exported*). |

Re-importing is safe: existing parties/items are updated (group, opening balance, unit, rate) — aliases,
addresses and bills you added in the app are kept. Tested with a 7,879-ledger list (≈4 s) and a
715-item stock summary.

## 3 · Daily use

### 📄 New Bill
| Tab | What to do |
|---|---|
| **📷 Photo / Upload** | Upload one or many bill photos (JPG/PNG/WEBP/PDF) or toggle *Use camera* (each shot is added to the list). Click **Read all N bill(s)**: every photo is read, then a summary table (file, party, date, bill no, lines, totals, status, issues) and one review card per bill appear. Save cards one by one (**Save this bill**) or all cards without open issues at once (**Save all ready (N)**). *Read more bills* adds to the batch; the cards stay on screen so nothing you typed is lost. |
| **🎤 Voice** | Pick the language (English-India, Hindi, Assamese, Bengali), press *Start recording*, dictate, press *Stop*. Check the transcript, click **Parse transcript**. You can also upload a WAV file. |
| **⌨️ Type / Manual** | Type the bill in plain words, or start a blank bill and fill the table by hand. |

Each read bill opens in its own **tab** (`1. Das Store`, `2. …`); voice / typed / manual entries are one-tab
batches. Below the items table a **totals row** shows *Items total (n lines)* next to the *Written on bill*
total with ✅ matches / ⚠️ difference so you can compare against the bill at a glance.

Then the **review tab** for each bill:

1. **Party check** – the name on the bill is looked up in the party master first:
   *Matched ✅* (exact or safe variant: case, "Stores/Store", "M/S", initials) · *confirm ❓* (close
   spellings listed with match %, e.g. "Gotam Kotna" → "Gautam Kotna"; numbered units like
   "20 Bn BSF" vs "200 Bn BSF" are never auto-matched) · *not in database 🆕* → fill group/address once,
   it is added on save and the bill's spelling is remembered as an alias.
2. **Bill details** – date (defaults to today if not on the bill – confirm), bill number (type it
   or *Auto-number* → `WI/2026-27/001`), Sale (Dr) / Purchase (Cr).
3. **Items** – editable table: item, qty, unit, weight (quintal), rate, amount, struck-out, note.
   Blank amount = weight × rate (or qty × rate). Crossed-out lines stay visible but are excluded.
3b. **Item check** – every description is checked against the items master:
   *Known ✅* (exact, case/punctuation variants, initials such as "Rice RR" → "Rice (R.R)"; never across
   different numbers or words — "Rice (R.R 10)" is not "Rice (R.R)") · otherwise a box per item where you
   choose **Use existing: …** (close matches with %), **➕ Add as NEW item** (name, unit, stock group) or
   **🔍 Use another existing item**. **Save stays disabled until every unknown item has a decision**; new
   items are created only then, and the bill's spelling is stored as an alias for next time.
4. **Totals & checks** – sum of lines vs total written on the bill; mismatches and missing data are
   flagged. Choose which amount to book if they differ. **Save this bill** / **Save all ready**.

A bill whose photo could not be read gets an error card (*Retry* / *Enter this bill manually*). Two cards
for the same new party create it once; a bill number already used for that party is refused per card.
Edits on the cards survive a visit to another page (the cards remember what you typed).

### 🏦 Bank Slips — deposit counterfoils → Contra / Receipt vouchers

Upload photos of pay-in slips (PNB counterfoils etc.) → **Read all**. Per slip Claude reads bank, branch,
date (the DDMMYYYY boxes), account number, account holder, amount in figures **and in words** (a
mismatch is flagged and the slip stays *needs input* until you correct the amount or tick *Confirm … – the
words are wrong*), cash / cheque, the cheque number and the cheque's bank (Tally's bank-allocation *Bank
Name* is the drawee bank, not ours). On each card you
confirm: amount, account number → **bank ledger name in Tally** (asked once per account number, then
remembered – see *Settings → Bank accounts*), **Receipt** (a party's payment: *Bank Dr / Party Cr*,
pick the party; default) or **Contra** (own cash deposited: *Bank Dr / Cash Cr*, cash ledger from
Settings), voucher number (*Auto-number* → `DEP/2026-27/001`) and narration. Saved slips are listed
below with date filters; **Build Tally files** writes the same five files as for bills and can mark
the slips as exported; a wrong slip can be deleted there.

### 📚 Bills & Export — files Tally can import

Right after **Save bill** the New Bill page offers the Tally files for that one bill (XML · Masters XML ·
Excel · CSV · zip). For batches use **Bills & Export**: filter by date / party / not-yet-exported, tick
the bills, choose the files, **Build files** → download each or the zip. Files are also saved under
`data/exports/`.

| File | What it is | How to import in TallyPrime |
|---|---|---|
| **Voucher XML** (`Tally_Vouchers_….xml`) | One Sales / Purchase voucher per bill: party ledger with a bill-wise *New Ref*, sales/purchase ledger, and one stock-item line per bill line (qty, unit, rate, amount). Tally's native import format. | Gateway of Tally → **Import → Transactions** → choose the XML |
| **Masters XML** (`…_masters.xml`) | The ledgers, units, stock groups and stock items those vouchers refer to. Duplicates are ignored (`@@DUPIGNORECOMBINE`), so existing masters are untouched. | **Import → Masters** → choose the XML — do this *before* the vouchers if any party/item is new to Tally |
| **Voucher Excel – Tally template** (`…_TallyTemplate.xlsx`) | Tally's official *Accounting Voucher* sample workbook (`fixtures/tally_templates/AccountingVouchers.xlsx`, sheet *Accounting Voucher* + its *Read Me*) with its 14 columns kept exactly as spelled (even the trailing space in `Change Mode `) and our extra Tally fields appended (*Quantity UOM, Bill Type of Ref, Bill Name, Bill Amount, Bill Amount - Dr/Cr, Reference No., Voucher Narration, Bank Allocations - …*). **Row layout:** one row per ledger entry; the voucher-level cells (*Voucher Date, Voucher Type Name, Voucher Number, Reference No., Change Mode*) are **repeated on every row of the voucher**, the narration is on the first row only; item rows carry *Item Name, Billed Quantity, Quantity UOM, Item Rate, Item Rate per, Item Amount* next to the sales/purchase ledger. Dates are real Excel dates. Bills and bank slips use the identical layout. Under *Settings → Excel import templates* you can upload your own copy of the template (e.g. exported from your TallyPrime): the export then fills **only the columns your file has, in your order, nothing appended**. | **Import → Transactions** → Excel — no column mapping needed |
| **Voucher Excel – row layout** (`Tally_Vouchers_….xlsx`) | Same vouchers, one row per entry (party row, then one row per stock item), headers *Voucher Date · Voucher Type Name · Voucher Number · Party Ledger Name · Ledger Name · Ledger Amount · Ledger Amount Dr/Cr · Bill Name · Bill Type · Stock Item Name · Quantity · Unit · Rate · Item Amount · Narration*; a *ReadMe* sheet explains the layout. | **Import → Transactions** → Excel → map the columns once, save the mapping template; Tally reuses it |
| **Voucher CSV** (`….csv`) | The same rows as CSV (UTF-8, dd-mm-yyyy). | Not imported by Tally directly — Tally's FAQ says to save it as .xlsx first. For spreadsheets / other software. |
| **Ledger master Excel** (`Tally_Ledgers_….xlsx`) | Parties + bill references in Tally's *Ledgers in Default Template* (+ an *Items* sheet). A custom masters template uploaded in Settings is followed the same way (columns addressed by header name; missing columns are left out). | **Import → Masters → Ledgers** |
| **Bank voucher files** (`Bank_Vouchers_….*`) | Contra / Receipt vouchers from the Bank Slips page in the same five formats. XML: bank ledger Dr (negative amount, `ISDEEMEDPOSITIVE` Yes) with `BANKALLOCATIONS.LIST` (transaction type *Cash* / *Cheque/DD* / *e-Fund Transfer* / *Others*, instrument no. and date, the instrument's bank); counter entry Cr (Cash ledger, or the party with an *On Account* bill allocation). Masters XML adds the bank ledgers under *Bank Accounts*. | as above |

**Before the first voucher import** set **⚙️ Settings → Tally voucher export settings**: your Sales /
Purchase ledger names and voucher types exactly as in Tally, optional company name and godown, and
whether vouchers carry stock items or are accounting-only (item details then go into the narration).
Weight-priced lines (`.52 × 5550`) export as `0.52 Qtl @ 5550/Qtl`; package-priced lines as
`5 Bag @ 1540/Bag`. If a stock item exists in Tally with a different unit the export warns you.

Tally accounting conventions used: Sales → party **Dr**, sales ledger **Cr**; Purchase → party **Cr**,
purchase ledger **Dr**; every voucher balances to zero; dates `YYYYMMDD`; amounts 2 dp; quantities up to 4 dp.

### 👥 Parties · 📦 Items
Edit names, groups, addresses, opening balances and aliases; merge spellings via aliases.

## 4 · Where things live

| | |
|---|---|
| Database | `data/accounting.db` (SQLite – back it up from Settings) |
| Bill images | `data/bill_images/` |
| Exports (XML / Excel / CSV) | `data/exports/` |
| Multi-user mode | `data/master.db` (accounts, plans, usage, settings) · `data/tenants/<user id>/` (that user's database, images, exports, templates) · `data/master.key` (encrypts stored API keys unless `AE_SECRET_KEY` is set) |
| Google sign-in | `.streamlit/secrets.toml` (see `.streamlit/secrets.toml.example`) |
| Settings | `.env` (see `.env.example`: business name, default state, bill prefix, model, data folder) |
| HTTPS certificate (Linux, `AE_HTTPS=1`) | `certs/` |

## 5 · Reading conventions Claude is told about

* `5 Bg Rice (BPT) 5 x 1540 7700` → 5 bags priced per bag.
* `2 Bg Rice (Sri) .52 x 5550 2886` → 2 bags, 0.52 quintal × ₹5550/qtl.
* Struck lines are listed with *Struck* ticked and excluded from the total.
* The small printed page number (13, 32, 02 …) is **not** used as the bill number.
* Dates are never guessed – you are asked.
* Known party and item spellings from your database are passed to Claude so it prefers them.

## 6 · Development

```
pip install -r requirements.txt
python -m pytest -q tests          # 121 tests (DB, Tally importers, matcher, validator, extractor, exporters, speech, tenants, master DB)
AE_EXTRACTOR_MODE=mock streamlit run app.py   # offline demo: sample bills return canned readings
```

Layout: `engine/` (pure logic, no Streamlit; `master.py` accounts/plans, `tenant.py` per-user folders, `runtime.py` per-request key/quota, `payments.py` Razorpay, `secrets.py` hashing/encryption) · `ui/` (pages; `auth_ui.py` login, `account.py`, `admin.py`, `theme.py`) · `app.py` (modes + navigation) ·
`fixtures/` (Tally template, 5 sample bills, mock extractions, sample Tally *List of Ledgers* / *Stock Summary* exports used by the tests) · `tests/`.
See `ROADMAP.md` for the build plan, decisions and what is next.
