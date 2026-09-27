# AccountVault

A local, offline credential manager that runs entirely on your own machine. There is no cloud, no sync and no account to sign up for. Your data never leaves the device.

It is a native desktop application built with PySide6 (Qt). No web server, no listening socket and no browser are involved.

## Features

**Accounts and categories**

- Built-in categories for Credit Cards, Banking, Loans, Investments, Income, Utilities, Websites and Secure Notes, plus any custom category name you type.
- Each category has its own fields. Credit cards track a credit limit, minimum payment, due date and interest rate; loans add a lender, payoff date and monthly payment; income tracks annual pay instead of a balance.
- A due date can be marked as repeating weekly, monthly, quarterly or annually, in which case the app works out the next occurrence itself rather than asking you to roll the date forward. New records in the categories that have a due date start out monthly; existing ones stay one-off until you say otherwise. Quick Update's button follows the cycle, so a quarterly bill moves on a quarter at a time rather than needing three presses, and an annual one does not need twelve.
- Each occurrence is worked out from the date on file rather than by stepping forward from the last one, so a bill due on the 31st shows as the 28th in February and goes back to the 31st in March instead of staying there.
- Sensitive fields (card numbers, account and routing numbers, security answers, note bodies) are masked by default with a show/hide toggle.
- The star beside an account's name pins it; the same star marks it in the list.
- Copy buttons sit beside usernames, passwords, websites, and the card, account and routing numbers. On a masked field the button copies the stored value without revealing it, so a card number can reach the clipboard without ever appearing on screen. Copying this way is also the only route that gets the clipboard protections below — a value selected by hand out of a revealed field does not.
- A built-in password generator sits under every password field, with a choice of length and character classes.
- Accounts can be marked closed. Closed accounts move to their own tab and drop out of the dashboard totals.
- Any account can be pinned. A Favorites tab appears while something is pinned and goes again when the last pin is removed, and pinned rows carry a star wherever they are listed. Closed accounts keep their pin — settling an account is not the same as no longer wanting it to hand.
- Deleting an account offers ten seconds to undo it, from the message that confirms the delete. Only closed accounts can be deleted at all, and after the window closes only a backup can bring one back.
- Select several rows with Shift or Ctrl click to close, reopen or move them to another category in one action.

**Finding things**

- Search runs across every category at once and shows which field matched, category names included. Sensitive fields are excluded from matching on purpose, so the search box cannot be used to confirm a secret you already suspect.
- Sort by name, due date or last updated, in either direction. The choice is remembered.
- A Needs Attention tab appears when something is due within 14 days, already past due, or has gone too long without an update. It stays hidden when there is nothing in it.
- How long is too long is per category, because it depends on what the figure measures: three weeks for a credit card or a bank balance, two months for a loan or an investment account, half a year for annual pay. Websites, secure notes and custom categories never go stale at all — a password is as correct as the day it was filed, and a note does not go out of date by being left alone.
- A one-off due date more than 60 days past reads as lapsed rather than urgent. The record is wrong rather than the payment overdue, so it is listed in neutral text instead of the red reserved for a bill you can still act on.

**Money tracking**

- Every balance change recorded on an account writes a dated entry to its balance history. Entries can also be added, edited and deleted by hand.
- Past balances can be imported into the selected account in bulk — paste two columns straight out of a spreadsheet, or pick a CSV your bank exported. The date and amount columns are worked out for you and named back to you before anything is written, along with a preview of every entry and a count of the rows that were skipped and why. Rows the account already holds are dropped, so importing the same statement twice adds nothing the second time.
- Dates are read in whatever form the file writes them — `2024-01-05`, `01/05/2024`, `Jan 5 2024`, or a full timestamp. A month on its own, `2024-04`, is filed on the first of that month, which is the one date the importer completes rather than reads; anything else it cannot fully determine is skipped and reported rather than guessed at.
- History can be exported back out as a CSV of dates and figures, oldest first, via the account's Export button (see below). Dates are written `YYYY-MM-DD`, the one spelling that cannot be read two ways, so an exported file imports back into the same days it came from.
- The dashboard builds a monthly net worth trend from that history, along with a per-category trend and the month-over-month change. Credit cards and loans count as liabilities; utilities, secure notes and income are left out of the totals.
- A composition bar splits each side into the categories that make it up, with every segment named and given its amount and its share. Assets and liabilities get a bar each rather than sharing one: they are opposite signs, so a single bar carrying both would be measuring nothing.
- The per-category cards are ordered largest first and each states its share of its own side, so the categories that move the total are the ones read first. Every card draws the same size chart whatever it holds, which is fine for reading a trend and useless for comparing two — the bar above is what compares them.
- Months with no reading carry the last known balance forward rather than dropping to zero.

**Vault files and backups**

- You choose where the vault lives. Create one anywhere, open another, and switch between them; the app remembers the ones you have opened.
- A backup is a copy of the vault file, so it opens with the same master password and there is no second password to lose. **Manage > Save a Copy** writes one on demand.
- Optional automatic backups, daily, weekly or monthly, into a `backups` folder beside the vault or anywhere you point them. Copies older than the retention window are removed, and the most recent one is always kept.
- Plaintext CSV export for spreadsheets, clearly labelled as unencrypted in the menu.
- Accounts can be imported from a CSV — this app's own export, another password manager's, or a spreadsheet of your own. The first row has to name the columns; which field each one is read as is listed before anything is written, along with the columns that matched nothing and were left out. Rows already in the vault are dropped, so importing the same file twice adds nothing the second time, and the whole file lands or none of it does.
- Each account has its own Export button offering three scopes: everything, the account details only (name, username, password, website and the category's own fields), or the balance history only. The choice matters, so the dialog states the consequence rather than just listing the options — the two scopes that carry the password say so behind a red warning and a button reading "Export With Password", and the history scope, which carries nothing identifying the account, does not. Balance history is the default; a category that tracks no balance is offered only its details.

**Session and appearance**

- Auto-lock after 5, 10, 15, 30 or 60 minutes of inactivity.
- Optional auto-shutdown after lock at 30 minutes, 1, 2 or 4 hours, or disabled.
- Clipboard auto-clear after 15, 30, 60 or 120 seconds.
- Dark and light modes, four font sizes, and a compact or comfortable list density.
- Window size, position and sidebar width are restored between sessions.

## Keyboard Shortcuts

| Keys | Action |
|---|---|
| `Ctrl+F` | Focus the search box |
| `Esc` | Clear an active search |
| `Ctrl+N` | Add an account |
| `Ctrl+E` | Edit the selected account |
| `Ctrl+B` | Copy the selected account's username |
| `Ctrl+Shift+C` | Copy the selected account's password |
| `Ctrl+1` to `Ctrl+9` | Jump to the first nine tabs, Dashboard included |
| `Ctrl++` | Larger text — one step up the four font sizes |
| `Ctrl+-` | Smaller text — one step down |
| `Ctrl+,` | Open Settings |
| `Ctrl+L` | Lock the vault |
| `Ctrl+Q` | Quit |

## Security

| Property | Detail |
|---|---|
| Key derivation | Argon2id at 128 MiB of memory, 4 iterations, 2 lanes. The parameters are recorded in the vault, so they can be raised later — the vault re-keys itself to the new figures at the next login |
| Field encryption | AES-256-GCM, with a unique random 96-bit nonce per field |
| Field binding | Every field is authenticated against the column it lives in, so ciphertext moved between columns, rows or tables stops decrypting. Without it, anyone who can write the database file can show a note as a password without knowing the key |
| Key storage | Memory only. Never written to disk, released on lock, but not scrubbed from RAM (see below) |
| Salt | 256-bit random value stored in the database |
| Key check | A token only the right key opens, verified at every unlock. A password and a salt that do not belong together otherwise log in successfully and read every field as unreadable — and the first save then writes that over the real value |
| Auto-lock | Configurable inactivity timeout. The key is released and decrypted data dropped on lock |
| Network | None. The app opens no sockets and makes no connections |
| Attack surface | A single process. Decrypted data never crosses a process boundary |
| Login throttling | 5 failed attempts trigger a lockout that doubles from 30 seconds to a 5 minute cap. Both the lockout and the running failure count are stored in the database, so restarting the app clears neither. The lockout is held as a start time plus a duration, and the remaining wait is capped, so moving the system clock cannot extend it |
| Clipboard | Copied secrets are cleared automatically, and only if the clipboard still holds what was copied |
| Database file | Restricted to the owning account: mode 0600 on POSIX, an explicit DACL on Windows with inheritance removed. Stored outside the application directory — see below |
| Deleted content | `secure_delete` scrubs pages as they are freed, so a deleted account does not linger in the file. The one-time format upgrade also compacts the file, which is what clears anything freed before that pragma was in force |
| Undo after delete | The ten-second undo holds the deleted rows still encrypted, so the window costs no more exposure than the vault file already does. It is dropped when the vault locks or changes — rows encrypted under one vault's key must never be restored into another's |
| Dependencies | Installed with `--require-hashes` against `requirements.lock`, so the artifacts are verified and not just the version numbers. A substituted `cryptography` or `argon2-cffi` wheel runs inside the process holding the master password |
| Master password | At least 15 characters, and may not begin or end with a space. It is the only password there is — a backup is a copy of the vault and opens with the same one |
| CSV export | Values that a spreadsheet would treat as a formula are neutralised before writing. A per-account export names its scope before writing and warns when the file will hold the password; its history scope carries dates and figures only |
| CSV import | The file is named column by column before anything is written, so a heading read as the wrong field is visible rather than discovered later. A column it cannot name is dropped, not guessed at. Nothing is decrypted or written until Import is pressed, and the dialog says the source file is still plaintext |
| Damaged data | A field that will not decrypt renders as a placeholder rather than raw ciphertext |

The master password itself is never stored. On login, Argon2id derives the AES-256-GCM key from the password and the stored salt, and that key is held in memory only.

Changing the master password re-encrypts every field in the vault under the new key. The operation is all or nothing: if any field fails to re-encrypt, the whole rotation is abandoned, because a vault half under the old key and half under the new one has no password that opens all of it.

### Where the vault is stored

You choose. **Create New Vault** opens a file dialog, and the vault is a
`.vault` file wherever you put it. The file itself is always restricted to the
owning account; the folder is not, because a folder you picked is yours and
locking down Documents on your behalf would break every other application that
uses it.

The default offered, and the location used by a vault created before this was
a choice, is the per-user application data directory —
`%LOCALAPPDATA%\AccountVault` on Windows, `~/.local/share/AccountVault`
elsewhere — and that directory *is* restricted to the owning account, because
the app created it.

Which vault was open last, and the handful opened before it, are recorded in
`vaults.json` in that same directory. It holds paths and nothing else — no
counts, no sizes, nothing summarising what a vault contains — and the recent
list can be cleared or switched off.

It is deliberately not kept beside the code. `init_db` can lock down the
database file, but nothing locks down the source tree, so a vault stored there
is protected by a permission on the data while the module that decrypts it sits
unprotected next to it: an attacker who can write `encryption.py` never needs to
read the database at all. Keeping the two apart also avoids an install under
`Program Files`, where the vault would be silently redirected by UAC
virtualization into a per-user shadow copy that neither backup tools nor the
user can find.

An existing `data/accountvault.db`, or one in the application data directory, is
adopted where it is and without asking, so upgrading moves nothing. Set
`ACCOUNTVAULT_DB`, or pass a path on the command line, to override the location
— for a vault on removable media, or a second one kept deliberately apart.

The application directory itself is worth putting somewhere other users cannot
write. No control inside the app can substitute for that: code that has been
modified is code that decides what the app does with your password.

### What locking does and does not do

On lock the app releases the key and drops the decrypted accounts it was holding, so nothing in the running process can decrypt anything further.

It does not scrub those bytes from RAM, and in Python it cannot. Strings and bytes are immutable, so there is no in-place overwrite, and a freed object keeps its contents until the allocator reuses the space. Further copies live inside `argon2-cffi`, inside `cryptography`, and in Qt's password field, where no Python code can reach them.

Anything the operating system pages out or writes into a crash dump is therefore outside the app's control. Full-disk encryption is what protects that at rest, and a short auto-lock timeout is what limits how long it exists at all.

### Why there is no local web server

Earlier versions served the UI over `http://localhost:8000`, which required session tokens, origin checks and CSP headers to keep other local processes and browser pages out. Running in-process removes that attack surface instead of defending it, so those mechanisms are gone.

## Requirements

- Python 3.10 or later
- pip
- A desktop environment. The app opens a window, and Qt supports Windows, macOS, X11 and Wayland.

## Installation

**Windows:** run `start.bat`. If the requirements are missing it offers to create a `.venv` folder and install them there, leaving the system Python untouched. If Python itself is missing it says so and prints the install command rather than failing silently.

**Linux / macOS:**

```bash
git clone <repo-url>
cd AccountVault
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
```

On Debian and Ubuntu the system Python is externally managed, so the virtual environment above is required rather than optional.

Package versions in `requirements.txt` are pinned, and Dependabot keeps them current through `.github/dependabot.yml`. `requirements.lock` pins the artifacts behind those versions, and `--require-hashes` is what verifies them — the check is worth having for an app that holds secrets, since a substituted wheel runs in the same process as the master password.

The lock covers the Windows and Linux x86-64 wheels. On any other platform there is no matching hash and the install is refused rather than quietly unverified; `pip install -r requirements.txt` works there, without that check. After a version bump, regenerate with `python tools/genlock.py` and commit the result — CI installs from the lock, so a stale one fails there rather than at somebody's first install.

## Running

**Linux / macOS:**
```bash
./start.sh
```

**Windows:**
```bat
start.bat
```

**Or directly:**
```bash
python main.py
```

The application window opens straight away. On first launch you are asked to create a master password of at least 15 characters. It is required every time you open the app and cannot be recovered if lost.

## Backup and Restore

A backup is a copy of the vault. It is the same encrypted file, so it opens with
the same master password, and restoring it is opening it — there is no import
step and no second password to keep track of.

Use **Manage > Save a Copy** to write one wherever you like. Turn on automatic
backups in **Settings** to have one written on unlock, daily, weekly or monthly.
They go to a `backups` folder beside the vault unless you choose somewhere else,
and they are named for the vault and the day: `mymoney_backup_20260917.vault`.

Old copies are removed once they pass the retention window — twelve months by
default, or never. Three things that never happen: a partial copy is never left
behind wearing a backup's name, nothing is deleted except immediately after a
new backup has been written and verified, and no file this app did not create is
ever deleted.

**One thing to know.** Changing your master password re-encrypts *this vault*,
not the copies. A copy taken beforehand still opens only with the password it
was taken under. The app offers you a fresh copy the moment the password
changes; take it.

Use **Manage > Move Vault** to move the vault somewhere else and carry on
working there. The original is left exactly where it was — delete it yourself
once you are satisfied the move worked.

**Manage > Export CSV** writes an unencrypted spreadsheet. It is useful for moving data elsewhere, and it is not a backup: anyone who opens the file can read every value in it.

## Tests

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/
```

The suite is weighted towards `services/`, `auth.py` and `encryption.py`, which are the paths where a bug destroys data that cannot be recovered: balance history, vault copies and their retention, the decrypted account cache, key rotation, login throttling, the legacy plaintext migration and the handling of fields that will not decrypt.

Most of `ui/` is deliberately not covered — its failures are cosmetic, and driving widgets costs more than it catches. The exceptions are the widgets that handle stored fields or secrets directly: the account form, which once destroyed fields it could not display, and the clipboard, which once kept a copied password after the vault locked. Those are tested like the services, which is why the suite needs `requirements.txt` installed too.

It runs on every push and pull request — see `.github/workflows/tests.yml`. That is what makes the exact pins safe to merge: Dependabot opens the bump, and this decides whether it is taken.

## File Structure

```
AccountVault/
├── main.py                  # Qt application entry point
├── database.py              # SQLite schema, connections, file permissions
├── encryption.py            # Argon2id key derivation + AES-256-GCM
├── auth.py                  # Master password, login throttling, session lock
├── preferences.py           # User settings and window view state
├── categories.py            # Field schemas, attention rules, formatting
├── vaultfile.py             # Which vault to open, and the recent list
├── urls.py                  # Blocks script-ish schemes before a link is opened
├── services/
│   ├── accounts.py          # Account CRUD, balance history, decrypted cache
│   ├── dashboard.py         # Monthly net worth and category trends
│   ├── csv_export.py        # The whole vault as a plaintext CSV
│   ├── account_import.py    # A table of accounts read back in
│   ├── backups.py           # Vault copies, the backup schedule and retention
│   ├── history_io.py        # Reads and writes one account's history as CSV
│   ├── account_export.py    # One account as CSV, at the scope the user picks
│   └── export_io.py         # Shared plaintext-file mechanics: cells, reads, writes
├── ui/
│   ├── main_window.py       # The widget tree, the account form, the dashboard
│   ├── actions.py           # Commands, shortcuts, the vault menu, appearance
│   ├── vault_session.py     # Which vault, locked or not, and everything on a clock
│   ├── account_list.py      # The list, the tab bar, search, sort, bulk actions
│   ├── auth_screens.py      # Vault chooser, first-launch setup, unlock
│   ├── detail_view.py       # Read-only account detail, per-account export
│   ├── account_form.py      # Add/edit form
│   ├── history_table.py     # Balance history table, chart and import
│   ├── dashboard_view.py    # Net worth and per-category trends
│   ├── dialogs.py           # Settings, password, account/history import, about
│   ├── widgets.py           # Charts, toasts, password generator, tabs
│   └── theme.py             # Colour palettes and the Qt style sheet
├── tests/                   # pytest suite — see Tests above
├── assets/
│   ├── accountvault.ico     # Window and taskbar icon
│   └── make_icon.py         # Regenerates the icon
├── tools/
│   ├── genlock.py           # Regenerates the hash-pinned lock files
│   └── make_demo_vault.py   # Builds a throwaway vault full of demo data
├── start.sh                 # Linux/macOS launch script
├── start.bat                # Windows launch script, installs if needed
├── requirements.txt         # Pinned versions
├── requirements.lock        # Pinned artifacts — what actually gets installed
├── requirements-dev.txt
├── requirements-dev.lock
└── .github/
    ├── dependabot.yml       # Watches the pins for releases and CVEs
    └── workflows/tests.yml  # Runs the suite on push and pull request
```

## Database Schema

### accounts

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, auto-increment |
| `category` | TEXT | Display name, for example "Credit Cards", "Banking", or a custom one |
| `name` | TEXT | Account name, **encrypted** |
| `username` | TEXT | **Encrypted** |
| `password` | TEXT | **Encrypted** |
| `website` | TEXT | **Encrypted** |
| `notes` | TEXT | **Encrypted** |
| `extra_data` | TEXT | Category-specific fields (balances, due dates, due-date recurrence, rates) plus the Favorites pin, serialised as JSON and **encrypted** as one blob. The pin lives here rather than in a column of its own so the file does not say which handful of accounts their owner reaches for most |
| `closed` | INTEGER | 0 for active, 1 for closed |
| `date_added` | TEXT | YYYY-MM-DD, set on insert. Not shown anywhere in the app |
| `last_updated` | TEXT | YYYY-MM-DD, rewritten on edit. Drives the staleness half of Needs Attention, against the interval its category declares |

### balance_history

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, auto-increment |
| `account_id` | INTEGER | Foreign key to `accounts.id`, cascading on delete |
| `balance` | TEXT | Numeric value, **encrypted** |
| `recorded_at` | TEXT | ISO 8601 date or datetime |

Each encrypted column holds `base64url(12-byte nonce || AES-256-GCM ciphertext || 16-byte tag)`.

### app_config

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT | Primary key |
| `value` | TEXT | |

Stored keys:

| Key | Default | Notes |
|---|---|---|
| `master_hash` | none | Argon2id hash of the master password |
| `salt` | none | Hex-encoded 256-bit salt used for key derivation |
| `kdf_params` | none | The Argon2id work factors the salt above was used with. Recorded so they can be raised without orphaning every existing field |
| `vault_format` | none | How the fields are encrypted. `1` binds each to its column; absent means a vault written before that, upgraded at the next login |
| `key_check` | none | A token encrypted under the session key, decrypted at unlock to confirm the key opens this vault |
| `lockout_started` | none | POSIX timestamp the current login lockout began at |
| `lockout_duration` | none | Length of that lockout in seconds. Stored with the start time rather than as a deadline, so the remaining wait can be capped and the system clock cannot be used to extend it |
| `failed_login_attempts` | 0 | Consecutive failed attempts, persisted so a restart does not hand back the attempts still left before a lockout |
| `lock_timeout_minutes` | 15 | Inactivity auto-lock timeout |
| `clipboard_clear_seconds` | 30 | Delay before a copied secret is cleared |
| `shutdown_after_lock_minutes` | 60 | Minutes after lock before the app exits, 0 to disable |
| `theme` | dark | UI theme, `dark` or `light` |
| `font_size` | default | UI font size |
| `list_density` | compact | How much room each account row takes |
| `sort_key` | name | Account list sort order |
| `sort_descending` | per sort key | Direction override for the current sort |
| `window_geometry` | none | Hex-encoded result of `QMainWindow.saveGeometry()` |
| `sidebar_width` | none | Sidebar width in pixels, accepted between 240 and 560 |

Everything below `key_check` is preference or view state rather than vault content, and is stored unencrypted. The lockout rows in particular have to stay legible while the vault is locked — which also means anyone who can write the database file can clear a lockout between guesses. That is not a gap the app can close, and it is why the Argon2id verify, not the lockout, is what actually bounds guessing; the lockout exists to slow someone typing at the keyboard.

## Dependencies

| Package | Purpose |
|---|---|
| `PySide6` | Qt bindings, the desktop UI |
| `cryptography` | AES-256-GCM encryption |
| `argon2-cffi` | Argon2id key derivation |

SQLite comes from the Python standard library. `pytest` is the only development dependency and is kept in `requirements-dev.txt`, so anyone who just wants to open their vault is never asked to install a test runner.

Every version is pinned exactly, transitive dependencies included. That is only safe with something watching for the next release, so `.github/dependabot.yml` opens a pull request when a pin falls behind or picks up a CVE, and `.github/workflows/tests.yml` runs the suite against the bump before it can be merged. The two crypto packages are deliberately left ungrouped, so each one arrives on its own and gets reviewed by itself.
