# AccountVault — Design Document

This records the shape of the application and the decisions behind it. It is
the "why"; the README is the "what and how" — features, installation, the full
security table and the exact database schema live there, and are not repeated
here.

Everything below describes the application as it is actually built. An earlier
version of this file described a FastAPI web application with one SQLite table
per account type and Fernet encryption. None of that was ever how the app
shipped, and the sections on encrypted fields were wrong in the direction that
matters — they listed most fields as unencrypted when in fact every one of them
is encrypted.

---

## 1. Stack

| Layer | Technology |
|---|---|
| Interface | PySide6 (Qt 6) desktop window |
| Application | Python, single process |
| Database | SQLite (Python standard library) |
| Field encryption | AES-256-GCM via `cryptography` |
| Key derivation | Argon2id via `argon2-cffi` |
| Storage | 100% local. No cloud, no server, no sockets |

Three direct dependencies: `PySide6`, `cryptography`, `argon2-cffi`. `pytest`
is development-only and kept in a separate requirements file, so someone who
just wants to open their vault is never asked to install a test runner.

---

## 2. Architecture

### Why a desktop app and not a local web server

Earlier versions served the UI over `http://localhost:8000`. That bought a
browser-based frontend and cost a defensive perimeter: session tokens, origin
checks and CSP headers, all needed to keep other local processes and stray
browser pages away from a service that would happily hand out decrypted
passwords.

Running the UI in the same process removes that attack surface rather than
defending it. There is no socket to reach, no request to forge, and decrypted
data never crosses a process boundary. The tokens and origin checks are gone
because there is no longer anything for them to protect against.

### Layers

```
main.py            Qt entry point, top-level error handling
  ui/              Widgets and window lifecycle. No SQL, no crypto
  services/        Account CRUD, vault copies, dashboard, CSV import/export
  auth.py          Master password, login throttling, session lock
  preferences.py   User settings and window view state
  categories.py    Per-category field schemas and formatting
  encryption.py    Argon2id key derivation, AES-256-GCM envelope
  database.py      Schema, connections, file permissions
```

**The window is four files, not one.** `ui/main_window.py` had grown to 2,900
lines holding at least five unrelated jobs, and it was the one place in the
codebase where the discipline the rest of it keeps was not kept. It is now the
coordinator — the widget tree, the account form's lifecycle, the
unsaved-changes guard, the dashboard toggle — and three mixins carry the rest:

```
  ui/actions.py        Commands, shortcuts, the vault menu, appearance
  ui/vault_session.py  Which vault, locked or not, and everything on a clock
  ui/account_list.py   The list, the tab bar, search, sort, bulk actions
```

Mixins rather than collaborators, and that is a real limit on what the split
buys. These methods reach all over the window, and wrapping them in an object
holding a reference back to it would invert nothing while costing an
indirection at every call. What it does buy is that the file you open to change
a shortcut is 400 lines about shortcuts, and that each of the four now states
at the top which slice of the window's state it owns — which is the thing that
was actually missing, since nothing previously said whether `self.accounts` was
the list's to clear or the session's.

**`services/backup.py` is `services/csv_export.py`.** It sat one character from
`services/backups.py`, and the two do opposite things: one writes every
password in the clear, the other copies the encrypted file. `from services
import backup` beside `from services import backups` is a typo that imports the
wrong module and raises nothing.

The dependency direction is one-way: `ui` calls `services` and `auth`, which
call `encryption` and `database`. Nothing lower imports anything higher.

Two rules hold this together and are worth stating because breaking either is
easy and silent:

- **Every AES-GCM envelope is built in `encryption.py`.** There is exactly one
  call to `os.urandom` for a nonce in the whole application. The guarantee that
  no nonce ever repeats under one key is only checkable while there is one site
  to check, so `encrypt` and `reencrypt` both route through
  the same helper rather than each constructing their own.
- **Every read and write of `app_config` goes through `database.read_config` /
  `write_config`.** These accept an optional open connection, which is what lets
  the password rotation write the new hash and salt inside the same transaction
  as the re-encrypted fields.

---

## 3. Security design

The full property table is in the README. The decisions worth explaining:

**Everything is encrypted, not just the obviously sensitive fields.** Account
names, usernames, websites, notes and all category-specific data are encrypted
alongside passwords and account numbers. The vault is not only protecting the
password to an account — the fact that a particular account exists at all is
worth protecting, and a name like "Chase Visa" sitting in plaintext next to an
encrypted number gives most of that away.

**Argon2id parameters are shared between key derivation and password
verification.** A stolen database exposes both the master-password hash and the
salt, and either one recovers the password, so an attacker works on whichever
is cheaper. Tuned separately the two drift apart and the pair is only ever as
strong as its weaker half. At the argon2-cffi defaults the verifier was roughly
four times cheaper to attack than the key derivation, which quietly capped the
vault's offline strength below what the derivation was already paying for.

**Raising those parameters is not a free upgrade.** The verifier stores its
work factors inside each hash string, so a change there is re-hashed on the
next successful login and costs nothing. Key derivation keeps no such record:
it rebuilds the session key from module constants at every unlock, so a new
value derives a different key and every existing field stops opening. Changing
them requires a migration that re-encrypts under the new key first.

**Rotation is all or nothing.** Changing the master password re-encrypts every
field in the vault. A vault half under the old key and half under the new one
has no password that opens all of it, so a field that will not decrypt aborts
the whole rotation and leaves the vault untouched, rather than being wrapped
again and losing the last copy of whatever it protected.

**Login throttling is enforced inside `auth`, not by its callers.** Both the
lockout and the consecutive-failure count are persisted, so quitting the app
clears neither. The lockout is stored as a start time plus a duration rather
than as an absolute deadline, and the remaining wait is capped — a deadline is
a fact about the system clock, and the clock is exactly what an attacker at the
keyboard controls.

**The key is released on lock, not wiped.** Python cannot do better: strings and
bytes are immutable, so there is no in-place overwrite, and copies exist inside
`argon2-cffi`, inside `cryptography` and in Qt's password field where no Python
code can reach them. The README says what this does and does not guarantee. The
vocabulary is deliberate — "release", never "wipe" — and it should stay that way
wherever this is described.

**File permissions are defence in depth, and are verified.** The database is
restricted to the owning account (mode 0600 on POSIX, an explicit DACL with
inheritance removed on Windows) and the result is read back rather than
assumed. A hardening step that can silently do nothing is worse than none,
because it is the one on the diagram that nobody re-checks. When it fails, the
app says so at launch instead of staying quiet.

---

## 4. Data model

**One `accounts` table, not one per account type.** The original plan had six
tables — creditcards, banking, loans, investments, utilities, websites — with
largely overlapping columns. Every feature that crosses categories would then
have to fan out across all of them: search, the dashboard, the password
rotation, the plaintext migration. Adding a category would mean a new table and
a new branch in each of those.

Instead, shared columns live on `accounts` and everything category-specific is
serialised to JSON in `extra_data` and encrypted as one blob. A new category is
a new entry in `categories.py` and nothing else.

The three tables are `accounts`, `balance_history` and `app_config`; their
columns are documented in the README.

**Categories are display names, and `categories.py` is their schema.** The
current set is Credit Cards, Banking, Loans, Investments, Income, Utilities,
Websites and Secure Notes, plus any custom category the user creates. Each
declares its fields, which of them are sensitive, which get a copy button,
which render as links, and how long a record may go untouched before it counts
as stale. Aliases map the old lowercase route-style names onto the current
display names so vaults written before the rename still open.

**`extra_data` holds due dates, their recurrence, and the Favorites pin.**
Promoting `due_date` to a real column was considered and declined: an encrypted
column buys nothing over the encrypted blob, and a plaintext one would cross
the line this design draws between structure and content.

The pin is in the blob for the same reason, and the reasoning is worth stating
because `closed` is a plaintext column and the two look alike from a distance.
They are not alike. `closed` is a lifecycle state deciding which view a row
belongs to at all, and the vault gives away about as much through the
unencrypted `category` column anyway. A pin is a statement about which five of
two hundred accounts their owner uses most — that is content, and a plaintext
column would hand it to anyone who can read the file. Keeping it encrypted
costs nothing: `list_accounts` already decrypts the blob on every call, so
filtering on it is free.

`services.accounts.set_favorite` writes it through a narrow update touching
`extra_data` alone. Going through `update_account` would re-encrypt every
column and force a balance snapshot, so pinning an account would file a history
entry claiming its balance had been re-read — the same reasoning that keeps the
bulk edits narrow, and the same reason nothing here moves `last_updated`:
pinning verifies nothing about what is inside the record.

**Recurring due dates are rebuilt from the anchor, never stepped forward.** The
cycles are weekly, monthly, quarterly and annual. The stored date is an anchor
and what recurs is its position — the weekday for a weekly cycle, the day of
the month for the rest — so the next occurrence is found by counting the whole
periods since the anchor and adding them in one go. Walking forward one period
at a time would be simpler and is wrong: the end-of-month clamp is one-way, so
a bill due on the 31st comes back from February as the 28th and stays there for
the rest of the account's life.

---

## 5. UI

**Master-detail:** category list and account list on the left, detail panel on
the right, tabs across the top. The window remembers its geometry, sidebar width
and sort order between launches — stored per-item rather than folded into the
settings bundle, so dragging a splitter does not rewrite the theme and all three
timeouts.

**Sensitive fields are masked by default**, with a show/hide toggle. The copy
button on a masked field copies the real value while it is still hidden, so a
card number never has to appear on screen to be used. Copied secrets are cleared
from the clipboard after a configurable delay, and only if the clipboard still
holds what was copied.

**A field that will not decrypt renders as a placeholder,** never as raw
ciphertext. Handing back the token would make ciphertext look like a real value:
it would render as the password, export into a CSV as the password, and be
written back as the password by the next save.

**A delete is final, and then it is not, for ten seconds.** The confirmation
stays — it and the undo catch different mistakes. The dialog catches the click
that was never meant; the undo catches the one that was meant and turns out to
have been wrong, which no amount of confirming prevents because the user
already believed they were sure. The dialog's wording changed with it: "this
cannot be undone" was true when there was nothing to undo it with, and
repeating it beside an Undo button would be the app lying about itself.

What the undo holds is the deleted row and its history exactly as they sat on
disk, still encrypted. Nothing is decrypted to build it, so the window costs no
more exposure than the vault file already does — and it is dropped by both
`close_vault` and `lock_vault`, because a restore token is rows encrypted under
one key, and offering it after a lock would write them into whatever vault is
open next as rows nothing there can open. An account made permanently
unreadable by a button labelled Undo is the worst outcome this feature has, and
it is the one that teardown exists to prevent.

The restored row reclaims its original id. `accounts.id` is `AUTOINCREMENT`, so
sqlite never hands the number out again, which both makes it free to take back
and keeps the `balance_history` rows pointing at the account they belong to.

**Accounts can be imported, not only exported.** The app could write its
accounts out three ways and take none of them back; the only route in was
typing. `services/account_import.py` is the other half, and it differs from the
balance-history importer beside it in one way that matters. History is two
columns and the parser can find them by looking at the values, because a date
looks like a date. An account is a dozen columns of free text, and nothing
about `jsmith` says whether it is a username or a security answer — so a header
row is required, and a column the importer cannot name is dropped and reported
rather than swept into `extra_data`. A wrong guess here does not produce a
visibly wrong chart; it files a password under `notes`.

Search, a net-worth dashboard, per-account and whole-vault CSV export, account
import, encrypted vault copies and scheduled backups, balance history with CSV
import, favourites, closed-account handling and light/dark themes are all
present; the README lists them.

---

## 6. Platforms

Windows and Linux from the start; macOS works as well, since Qt supports it and
nothing in the app is Windows-specific. Paths are resolved with `pathlib` and
anchored to the source file rather than the working directory, because a
shortcut, `start.bat` and a shell all launch from somewhere different.

The two platform-specific paths are file permissions (`chmod` versus `icacls`)
and the Windows taskbar identity. Both are guarded with `os.name`, and both are
best-effort: a vault on a filesystem that cannot express owner-only permissions
must still open.

---

## 7. Testing

`pytest` covers `services/` and `auth.py` — the paths where a bug destroys data
the user cannot get back.

Most of the Qt layer is deliberately not covered: its failures are cosmetic,
and driving widgets costs far more than it catches. The exception is any widget
that handles a stored field or a secret directly, which gets the same treatment
as a service — the account form once destroyed stored fields on save, and
locking the vault once left a copied password on the clipboard. Both are data
handling that happens to live in a widget. `tests/test_ui.py` states that rule
at the top of the file and is the place to check it; this section used to claim
the Qt layer had no coverage at all, which stopped being true some time before
anyone updated it.

The suite runs the key derivation at its floor via a fixture, since the real
parameters cost about a second per derivation and nothing in those tests is
checking the work factors. The work factors are asserted directly, without that
fixture, in `test_rotation.py`.
