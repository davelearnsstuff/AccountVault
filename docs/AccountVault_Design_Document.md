# AccountVault design document

- This is the "why" behind the app. The README covers the "what" and "how": features, installation, the full security table (**Security details**), and the database layout (**Database schema**).
- It describes the app as it's built today.

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

- There are three runtime dependencies: `PySide6_Essentials`, `cryptography`, and `argon2-cffi`. Essentials rather than the full `PySide6`, which also installs Qt WebEngine (a whole Chromium), Qt Pdf, and other add-ons the app never loads.
- `pytest` is in a separate development requirements file, so people who just want to open their vault never install a test runner.

---

## 2. Architecture

### A desktop app, not a local web server

- Older versions served the interface at `http://localhost:8000`. That needed session tokens, origin checks, and CSP headers to keep other programs and web pages away from decrypted passwords.
- Running the interface in the same process removes that risk entirely. There's no socket to reach and no request to forge, and decrypted data never leaves the process.

### Layers

```
main.py              Qt entry point, top-level error handling, error log
  ui/                Widgets and window lifecycle. No SQL, no crypto
  services/          Account CRUD, balance history, vault copies, dashboard,
                     retirement plan, CSV import/export
  core/              No Qt, and nothing above it is imported
    auth.py          Master password, login throttling, session lock,
                     vault format upgrades and re-keying
    encryption.py    Argon2id key derivation, AES-256-GCM envelope
    database.py      Schema, connections, file permissions
    preferences.py   User settings, view state, the backup seal,
                     warning settings, the stored retirement plan
    categories.py    Per-category field schemas, warning rules, formatting
    retirement.py    The retirement planner's math: tax, withdrawals,
                     simulated markets
    vaultfile.py     Which vault to open, and the recent list
    urls.py          Blocks script-ish link schemes before a link is opened
    version.py       The application version
```

- Imports only go downward. `ui` uses `services` and `core`, `services` uses `core`, and `core` imports nothing above it.
- `tests/test_layers.py` enforces the `core/` rule. It reads the source instead of importing it, so an import inside a function counts too.
- Nothing else would catch a slip. The tests have Qt installed, so a Qt import in `core/` passes them, but `tools/make_demo_vault.py` builds vaults with no window and would suddenly need a display.

### The main window is four files

- `ui/main_window.py` had grown to 2,900 lines doing at least five jobs. It's now the coordinator: the widget tree, the account form, the unsaved-changes guard, and the dashboard toggle.
- Three mixins hold the rest:

```
  ui/actions.py        Commands, shortcuts, the Manage menu, appearance
  ui/vault_session.py  Which vault, locked or not, and everything on a timer
  ui/account_list.py   The list, the tab bar, search, sort, bulk actions
```

- They're mixins, not separate objects, because these methods reach all over the window. A helper object holding a reference back to the window would add a step to every call and fix nothing.
- The real gain is that each file is a few hundred lines on one subject, and each says at the top which part of the window's state it owns.

### `csv_export.py` is deliberately not named `backup.py`

- `services/backup.py` would be one letter away from `services/backups.py`, and the two do opposite things. One writes every password in plain text; the other copies the encrypted vault.
- Mixing up `from services import backup` and `from services import backups` would import the wrong module without any error.

### Two rules that are easy to break without noticing

- **Every AES-GCM envelope is built in `encryption.encrypt_with`.** It's the only place a nonce is generated. With a single place, anyone can check that no nonce ever repeats under one key. `encrypt`, `reencrypt`, and the re-keying in `auth.py` all go through it.
- **Every write to `app_config` goes through `database.write_config`**, and single-key reads go through `read_config`. These accept an open connection, so a re-key can write the new hash, salt, key check, and encrypted config rows in the same transaction as the re-encrypted fields.

---

## 3. Security

The README's **Security details** table lists every property. These are the decisions behind them.

### Everything is encrypted

- Names, usernames, websites, notes, and all category fields are encrypted, not just passwords and account numbers.
- Why: the fact that an account exists is worth protecting. "Chase Visa" in plain text next to an encrypted card number gives most of it away.

### Each field is tied to its column and its row

- Each value is encrypted with its table, column, and row as associated data, such as `accounts.password:17`. A balance entry names its account and itself: `balance_history.balance:17:203`.
- Why the column: without it, anyone who can edit the file could move a note's encrypted value into the password column, and the app would show and copy the note as the password.
- Why the row: without it, they could swap two accounts' passwords, websites, or routing numbers, or move a balance entry onto another account. A balance entry is tied to itself as well as its account, so it can't be copied as a new entry either.
- A field can't be encrypted until its row has an id, so a new row is inserted with empty fields and filled in within the same transaction.
- Ids survive everything that keeps ciphertext as it is. Undo puts a deleted row back under its own id, and a backup is a copy of the file. Import and the CSV round trip encrypt fresh.
- The app_config rows (warning settings, retirement plan, backup seal, key check) keep column binding only. Each is a single row, with nothing to swap it with.

### Values are padded

- AES-GCM keeps the plaintext's length, so the file used to show every password's length and how many digits each balance had.
- Each value is now stored as a 4-byte length, the value, and zeros up to a multiple of 64 bytes. Most passwords, usernames, and balances fit in one block and look identical. A long note still shows its size, to the nearest 64 bytes.
- 64 bytes is a trade-off: smaller blocks would hide less, and larger ones would grow the file for little gain.
- Padding arrived with format 3, at the same time as row binding, so the vault is re-encrypted once for both.

### The key and the password check use the same Argon2id settings

- A stolen database contains both the password hash and the salt, and an attacker can go after whichever is cheaper to crack.
- At the argon2-cffi defaults, the hash was about 4 times cheaper to attack than the key derivation. That quietly capped how strong the vault really was.

### Argon2id settings are saved in the vault, so they can be raised

- The password hash stores its own settings and is re-hashed at the next login when they change.
- The key's settings are saved in `kdf_params`. Unlock uses the saved settings. If they're older than the current ones, `migrate_vault_format` re-keys the vault with a new salt during that same login, the only time the password is available.
- Saved settings are read before anything is verified, so values outside sane limits are ignored. Otherwise a crafted vault could ask for 64 GiB of memory and freeze the computer that opens it.
- The password hash carries its own settings, and checking a password runs them. So the hash is held to the same limits before it's checked, and must be Argon2id. A hash outside them is refused with "password record isn't valid," in the chooser and at login. It isn't counted as a failed login, since no password would get past it.
- `LEGACY_KDF_PARAMS` reproduces the key for vaults made before settings were saved. **Never edit it** to match the current settings.

### Format upgrades happen at login

- `vault_format` 0 has no column binding. Format 1 adds column binding, saved Argon2id settings, and a key check. Format 2 encrypts the category column too (see **The category is encrypted**). Format 3 ties each field to its row and pads it (see **Each field is tied to its column and its row**).
- An older vault is upgraded before any field is read. This version reads every field with its column binding, so an un-upgraded vault would look completely unreadable, and the first save would write that back.
- A single field that won't decrypt is skipped, since it's already lost. If *nothing* decrypts, the key is wrong, so the login fails instead of rewriting a vault that could still be recovered.
- The file is compacted once after the upgrade, because `secure_delete` only scrubs data deleted after it was turned on.
- Plaintext in a column is encrypted by the upgrade only if the vault is older than the format that started encrypting that column: format 1 for most columns, format 2 for the category. It's the only place plaintext is ever encrypted (see **Plaintext in a current vault is never trusted**).
- The key check is required from format 1 on, the format it arrived with, not just at the current format. Otherwise a vault could claim to be format 1 to skip it.

### The category is encrypted

- It used to be stored in the clear as "structure." But a custom category's name is your content (which is why the warning settings, keyed by category, were already encrypted), and the count per category says what kinds of accounts you have.
- Sorting by category moved from SQL into Python. The decrypted list is already in memory, so this costs nothing noticeable.
- A bulk move encrypts each row separately. One shared token would show on disk which accounts share a category.
- An unreadable category is kept as stored when the account is saved, the same as any other field (see **Unreadable fields show a placeholder**).
- Still readable without the password: how many accounts there are, closed flags, dates, and which balance entries belong to which account. Sorting, **Needs Attention**, and the login screen depend on them. Value lengths are hidden by padding (see **Values are padded**).

### Plaintext in a current vault is never trusted

- Once a vault is at the current format, every encrypted column should hold ciphertext. Plaintext there can only come from someone who edited the file without the password.
- It shows as unreadable, the same as a broken field. Showing it as the value would let that person plant a website, a note, or a routing number.
- It's never encrypted. The app used to encrypt leftover plaintext at every login, which turned a forged value into real ciphertext that looked exactly like the user's own data.
- A password change refuses, the same as for a broken field (see **Changing the password is all or nothing**). A work-factor re-key at login skips it.
- After unlocking, the app counts these fields and warns if there are any, so placeholders aren't mistaken for damage.

### A vault with database code in it is refused

- SQLite keeps triggers and views inside the database file and runs triggers on every save. Someone who could write the file once could plant one that puts a forged value back after every edit, survives a password change, and gets copied into every backup.
- The app creates none, so the schema is checked on every connection, not just when the vault opens. A vault on a shared drive can gain a trigger while it's open, and the next save is refused before the trigger can run.
- Anything the app didn't create is refused: a trigger, a view, an unknown table, or an index other than SQLite's own. The vault shows as "changed by another program" in the chooser, and a backup of it is refused.
- The one exception is the seven per-category tables from the first design (see **One `accounts` table, not one per category**). The move to one table never dropped them, and a table holds data but runs nothing.
- Every connection also turns on SQLite's own protections: `trusted_schema` off and defensive mode (Python 3.12 and later). These narrow what a schema could do if one ever got past the check.
- Refusing is simpler than repairing, and nothing legitimate creates these objects.

### The folder is checked, and the journal is kept

- Restricting the vault file doesn't stop anyone who can write its folder. They can replace the file, or plant a journal that SQLite replays as crash recovery the next time the vault opens.
- A folder the user chose isn't the app's to restrict (locking down Documents would break other apps), and a real crash leaves the same kind of journal, so neither can be blocked. The app finds out and warns instead.
- The check runs every time a vault opens: at startup, from the chooser, after creating one, and after a move. It counts any account other than the owner, SYSTEM, and Administrators that can create, delete, or rename files there. SYSTEM and Administrators can take over any file anyway.
- On Windows the folder's access list is read through the Windows API. `icacls` can't be used for this, because it prints account names in the display language.
- Only allow entries are read, so a folder can be reported open when a deny entry actually closes it. It's never the other way around.
- SQLite normally creates its journal for each save and deletes it after, so each one gets the folder's permissions. During a save it holds the vault's pages, with app settings in plain text.
- The app now creates the journal once, owner-only, and runs SQLite in `TRUNCATE` mode, which reuses the file and empties it after each save. An empty journal isn't a crash journal, so it changes nothing about recovery.

### A key check catches a key that opens nothing

- The password hash and the salt are separate rows. If they get out of sync (a half-restored backup, or a forged hash), the password is accepted but the key opens nothing. Every field shows as unreadable, and the first save overwrites the real value.
- A known value encrypted with the vault key is checked at every unlock, which turns that into a refused login.

### Changing the password is all or nothing

- Every field is re-encrypted with the new key. If any field won't decrypt, the change is canceled and the vault is left untouched, because a vault split across two passwords can't be fully opened with either.
- This is the opposite of the login upgrade, on purpose. A password change can wait until the field is fixed; a login can't.
- Both re-key paths also re-encrypt the encrypted `app_config` rows (the warning settings, the retirement plan, and the backup seal) in the same transaction. Otherwise those rows would be left under a key nothing can produce anymore.

### The backup settings are sealed

- Anyone who can edit the vault file can change `app_config` without the password. The backup rows are instructions: where to copy the whole vault and which files may be deleted. Unprotected, they'd let someone with write access to a shared or synced vault send the owner's next backup to their own folder.
- So a seal covering every backup row is encrypted with the vault key. The rows stay readable, because the locked window needs them, but only someone who can open the vault can make a matching seal.
- If the seal doesn't match, the settings are reset to the defaults (automatic backups off) and the user is told. Keeping them would defeat the point, since deleting the seal is as easy as forging the rows.
- A settings save only re-seals rows this vault had already sealed, so it can't approve rows changed behind the app's back.
- The seal is checked before a backup folder is shown for approval, so a single click can't approve a forged folder.

### Warning settings are encrypted

- Other settings (theme, timers, window layout) are stored in plain text because the window has to be built before login, and none of them are vault content.
- Warning settings name custom categories, which is vault content. They're only needed while the vault is open, so encrypting them costs nothing, and a forged value fails to decrypt and falls back to the defaults.
- They're dropped on lock, so the next vault opened doesn't inherit them.

### Login throttling lives inside `auth`

- `verify_master_password` and `change_master_password` enforce it themselves, so a new caller can't skip it.
- The lockout and the failure count are both saved, so quitting the app resets neither.
- The lockout is stored as a start time and a length, and the remaining wait is capped. An end time would depend on the system clock, which someone at the keyboard controls.
- The escalation counter is only kept in memory, so a restart drops the next lockout back to 30 seconds. That limits how long a mistyped password can lock out the owner.
- The lockout rows are plain text because they're needed while the vault is locked, so anyone who can edit the file can clear them between guesses. Nothing can prevent that. Argon2id makes each guess expensive; the lockout only slows down someone at the keyboard.

### The key is released on lock, not wiped

- Python can't overwrite strings and bytes in place, and copies live inside `argon2-cffi`, `cryptography`, and Qt's password field, out of reach.
- The README's **What locking doesn't do** explains what this means for users.
- Always say "release," never "wipe," wherever this is described.

### File permissions are checked, not assumed

- The vault file is restricted to its owner (mode 0600 on Linux and macOS, and an access list without inherited permissions on Windows), and the result is read back.
- A hardening step that can fail silently is worse than none, because nobody re-checks it. If it fails, the app says so at launch.
- A folder the user picked isn't touched, because locking down a folder like Documents would break other apps. Only the app's data folder and folders the app has just created are restricted.

---

## 4. Data model

### One `accounts` table, not one per category

- The original plan had six tables with mostly the same columns. Search, the dashboard, password changes, and the plaintext migration would all have had to loop over every table, and each new category would have needed a new table and changes in all of those places.
- Instead, shared columns live on `accounts`, and everything category-specific is stored as JSON in `extra_data`, encrypted as one value. Adding a category is just a new entry in `categories.py`.
- The tables are `accounts`, `balance_history`, and `app_config`. The README's **Database schema** lists their columns.

### `categories.py` is the schema

- Categories are display names: Credit Cards, Banking, Loans, Investments, Property, Income, Utilities, Websites, Secure Notes, and any custom ones.
- Each category lists its fields, which ones are hidden, which get a copy button, which are links, and how long a record can go without an update.
- Aliases map the old lowercase names to the current ones, so older vaults still open.

### Due dates stay in `extra_data`

- Making `due_date` its own column was considered and turned down. An encrypted column adds nothing over the encrypted JSON, and a plain-text one would expose content.

### The Favorites pin is encrypted, but `closed` isn't

- `closed` decides which view a row appears in, and it reveals little more than the plain-text `category` column already does.
- A pin says which few accounts, out of maybe hundreds, the owner uses most. That's content, so it goes in the encrypted JSON. `list_accounts` already decrypts that on every call, so filtering on it is free.
- `set_favorite` only updates `extra_data`. Going through `update_account` would re-encrypt every column and add a balance history entry, as if pinning had re-checked the balance. For the same reason, pinning doesn't move `last_updated`.

### Balance history sets the balance

- Every history change recalculates the current balance from the newest entry. An entry dated on or after all the others is the current balance; an older one only fills in the past.
- That one rule covers the history table, the CSV import, and a backdated Quick Update, so they can't disagree about which figure is current.
- Deleting a wrong newest entry puts the balance back to the one before it.
- `last_updated` moves forward with the newest entry, never back. Filing an old statement doesn't make an account look more out of date.

### When a save adds a history entry

- Quick Update and **Mark Checked** are readings of the balance, so they add an entry even when the figure hasn't moved. That includes a Quick Update that only moves the due date.
- Both skip the entry only when the day it files under already ends on the same figure, so saving or clicking twice adds one entry. They check that day's last entry, not all of them: after 500 then 600, a new 500 is filed, or the account would read 500 while its history ended on 600.
- **Mark Checked** still moves `last_updated` to today when it skips the entry. The click is a check either way.
- The full edit form adds an entry only when the balance changes. Most of its saves are a note or a password, which aren't new readings.

### A Both account keeps two amounts that add up to its balance

- Investments accounts of a type that doesn't settle its own tax treatment (401(k), 403(b), 457(b), IRA, SEP IRA, and SIMPLE IRA) have a **Tax Treatment**: Traditional, Roth, or Both.
- A Both account stores a Traditional amount and a Roth amount, in dollars, and its balance is their sum. Its contributions are split the same way.
- Why dollars: statements show each part in dollars. A percentage would have to be worked out by hand and would drift as the balance moved.
- The balance stays the one figure that history, the dashboard, and net worth read. Only the retirement planner needs the parts.
- Entering a mixed account as two accounts was considered and turned down. The login would be stored twice, and every statement would take two Quick Updates.
- A balance history for each part was considered and left for later. History, Quick Update, the charts, and history import all assume one figure per account.
- Tax Treatment has no default, for the same reason the contribution period has none.

### The parts have no history

- The two amounts are stored with the account, latest only. Balance History can change the balance without them, by editing or deleting an entry or importing a newer one.
- So a new balance is only worked out from the parts when the parts change. A save that leaves them alone keeps the balance it was given. Otherwise editing a note would put the balance back to the old parts' sum and file that as a new reading.
- When the parts no longer add up to the balance, the edit form says so.
- A backdated Quick Update files its parts with its figure. They replace the account's own only if that figure becomes the current balance, the same rule the balance follows (see **Balance history sets the balance**).

### Some fields only apply when another field allows them

- A field can be limited to records where another field holds certain values (`shown_when` in `categories.py`). Tax Treatment only applies to the account types that need it, and the two amounts only to a Both account.
- The reverse, `hidden_when`, drops a field for certain values and keeps it for everything else. Annual Pay uses it, so a blank Income Type, or one an import spelled its own way, keeps its pay. Listing every type that has pay would clear the pay of any record not on the list.
- A field that stops applying is hidden and cleared on save, so an old amount can't come back later and change the balance. Fields from another category's schema are kept, as before.
- The form hides these fields without rebuilding itself, so anything typed into one comes back if it applies again before the save.

### Two checkboxes leave an account out, each of one thing

- **Exclude from Dashboard** takes an account out of every dashboard figure. **Exclude from Retirement** takes it out of the retirement planner. Neither affects the other.
- They're separate because they answer different questions. A child's 529 is the household's money, so it belongs on the dashboard, but it isn't for retirement. An account kept for a parent is neither.
- A dashboard tick leaves the account out of everything there, the investing rate included. Leaving it in some figures and not others would make the totals disagree with each other.
- A tick is stored as `Yes`, the way Auto Pay is, not as true. Exports write it as text, and the import reads any yes word back as `Yes`. Unticked, it's blank.
- Being out of the dashboard or the planner doesn't hide the account anywhere else. It still shows in its list, in search, and in Needs Attention.

### Select options are only ever added

- The edit form shows a stored value it has no option for as blank, and saving the account writes the blank over it.
- Until the form keeps such values, options can be reordered or added to, but never renamed or removed. A test checks that every Account Type ever offered is still offered.

### Repeating due dates are calculated from the original date

- The stored date is the starting point. The next due date is found by counting the whole periods since then and adding them all at once.
- Stepping forward one period at a time would be simpler but wrong. A bill due on the 31st would turn into the 28th in February and stay there for good.

### A vault is one file, and a backup is a copy of it

- A backup opens with the same password, and restoring one is just opening it. A separate backup password or format would be one more secret to lose and one more code path to trust.
- The catch is that a copy also carries the original's list of backup files. Opened as a live vault, it would delete the original's backups. So the list records which vault path it belongs to, and a vault that finds a different path there clears it.
- The only state outside any vault is `vaults.json` in the user's data folder, and it holds paths only.
- Opening a missing file and creating over an existing one are both refused. If a recent vault had moved, quietly creating an empty one in its place would look exactly like the vault being wiped.

---

## 5. Interface

### Layout

- Category tabs run across the top, the account list with search and sort is on the left, and account details are on the right.
- Window size, sidebar width, and sort order are each saved on their own, separately from the other settings, so dragging a divider doesn't rewrite the theme and timers.

### The vault locks with the computer, and sleep counts as idle

- Idle time is measured by both the wall clock and the monotonic clock, and the longer one counts. The wall clock can be set back, which would stretch a session. The monotonic clock can't, but on Linux and macOS it stops while the computer sleeps, so a vault left open overnight would wake as if seconds had passed.
- On Windows the vault locks when the session locks and when the computer is about to sleep (`WM_POWERBROADCAST`, `PBT_APMSUSPEND`), so it's never on screen unlocked when the lid opens.
- On Linux it listens on D-Bus for the same two moments: a screen saver going active (`org.freedesktop.ScreenSaver` for KDE and most desktops, `org.gnome.ScreenSaver` for GNOME) and logind's `PrepareForSleep`. A desktop that sends none of them falls back to the idle lock. This was written to the D-Bus specifications and hasn't yet been run on a Linux desktop.
- macOS isn't covered. It would need the Objective-C notification center through `ctypes`, and there was no Mac to test it on.

### Hidden fields and the clipboard

- The copy button on a hidden field copies the real value without showing it, so a card number never has to appear on screen.
- Copied secrets are cleared after a set time, but only if the clipboard still holds what was copied.
- They're also cleared when the app quits or the session ends (logging off, shutting down), not just when the window closes. Windows keeps clipboard data after the program that put it there exits, so a secret left there would outlive the app. A hard crash can't be caught.
- Besides the Windows clipboard-history and cloud-sync opt-outs, a copied secret carries `x-kde-passwordManagerHint`, which keeps it out of KDE's clipboard history.
- Only the copy buttons and copy shortcuts work this way. Text selected and copied by hand never passes through the app, so the app can't clear it.

### The security settings are sealed with the backup settings

- Settings are stored unencrypted, because the window is built before anyone logs in. Four of them decide how exposed an open vault is: the lock timeout, clipboard clearing, auto-shutdown after a lock, and screen capture. Anyone who can edit the file could weaken them without the password.
- So they're covered by the seal the backup settings already had: a token over the exact stored values, encrypted with the vault key. Theme, font size, and list density stay unsealed. They only change how the window looks.
- The seal is checked as soon as the vault is unlocked, before any of the four is acted on. If it doesn't match, they're reset to their defaults along with the backup settings, and the app says so.
- Before that check the stored values aren't believed. Screen capture stays blocked, and auto-shutdown at a launch straight to the login screen is never longer than the default, so "Never" can't be planted. After a lock, the values checked during the session are used.
- A vault sealed before these settings joined has a seal over the backup settings only. It's accepted once and reissued over the full set, so the upgrade doesn't reset anyone's settings. It can't be replayed later: the format 3 upgrade re-keys every vault and reissues the seal over the full set under the new key, so any older seal is under a key nothing can derive.

### Windows are kept out of screen captures

- Any program can capture any window, and Windows Recall does it every few seconds and keeps a searchable history. A revealed password or the account list would end up there. The clipboard is already kept out of its history, and this does the same for the screen.
- One app-wide filter applies the setting to every top-level window as it first appears, so dialogs, menus, and tooltips are covered without code of their own. Windows that are already open are updated when the setting changes.
- Windows uses `SetWindowDisplayAffinity` with `WDA_EXCLUDEFROMCAPTURE`, falling back to `WDA_MONITOR` (shown black) before Windows 10 version 2004. macOS sets `NSWindow.sharingType` through the Objective-C runtime with `ctypes`, so it needs no extra package. Linux has no equivalent, and Settings says so.
- On by default. **Allow screenshots and screen sharing** in **Settings** turns it off, for a bug report or a call. It's saved per vault like the other settings. With no vault open, the default applies.
- The setting is stored unencrypted, but sealed with the other security settings (see **The security settings are sealed with the backup settings**), so someone who can edit the vault file can't turn captures back on. Until the seal has been checked after an unlock, captures stay blocked, which keeps the login screen out of captures too.
- Whether a platform is supported is decided by Qt's platform name, not the OS. The tests run on Qt's offscreen platform, which never passes a made-up window handle to a real window system.
- The Windows calls were checked against a real window: a window and a dialog are excluded from their first show, and the setting switches both ways while they're open. The macOS path hasn't been run on a Mac.

### Unreadable fields show a placeholder

- Showing the raw encrypted text would make it look like a real value. It would display as the password, export as the password, and be saved back as the password.

### Stored values are shown as plain text

- Qt treats text that looks like HTML as formatting, so a password like `xK9<b>mq2` would show up bold and missing its tag.
- Stored values go through `plain_label`, which forces plain text.
- Tooltips can't be set to plain text, so any that quote a stored value or a file path go through `plain_text`. A vault file named with HTML tags would otherwise render as HTML in the chooser.

### Links are opened only when they're full web or mail addresses

- A website value goes to the desktop only through `normalize_url`, which refuses script-ish schemes and adds `https://` to a bare host name.
- A `mailto:` link keeps only its address. Everything after `?` is fields for the mail client, and some clients have honored ones that attach a file from the computer (`attach=`). A website value can come from an import, so it gets no say in what a new message carries.
- In the Readme and Design Document viewers, an in-page anchor scrolls to its heading and any other relative link is ignored. Passed to `normalize_url`, `docs/x.md` would have opened `https://docs/x.md`.

### CSV cells that only look like text are marked too

- A cell is marked as text (a leading apostrophe) when it starts with `=`, `+`, `-`, `@`, a tab, or a carriage return, the characters a spreadsheet reads as the start of a formula.
- The check skips leading spaces and uses the character's compatibility form (NFKC), so a full-width `＝` or a space before `=` is marked too. Whether current spreadsheets act on those hasn't been confirmed. Marking one costs an apostrophe the spreadsheet hides anyway.
- The import takes the apostrophe back off with the same test, so every value survives a round trip exactly.

### Delete has both a confirmation and a 10-second undo

- They catch different mistakes. The confirmation catches a click you didn't mean, and the undo catches one you meant and then regretted.
- The confirmation no longer says "this cannot be undone," since that would be false next to an Undo button.
- The undo keeps the deleted row and its history exactly as stored, still encrypted, so nothing extra is exposed.
- It's thrown away when the vault locks or closes. Otherwise it could restore rows encrypted with one vault's key into a different vault, where nothing could ever open them.
- A restored account gets its original id back. `accounts.id` is `AUTOINCREMENT`, so SQLite never reuses it, and the history rows still point to the right account.

### Account import needs a header row

- Balance history can be recognized from the values alone, because a date looks like a date. Account columns are free text, and nothing about `jsmith` says whether it's a username or a security answer.
- So the first row must name the columns, and a column that can't be matched is skipped and reported. A wrong guess here wouldn't just make a chart look off; it would file a password under notes.

### Imported dates are never guessed

- Dates with slashes are read month first, matching how the app shows dates, unless the first number is over 12.
- A bare month like `2024-04` is the only thing filled in, as the 1st of that month.
- Any other unclear date is skipped and reported, because a wrong guess would file a balance under the wrong month with no sign of it.
- Exports use `YYYY-MM-DD`, which can't be read two ways, so an exported file imports back to the same dates.

### Exports say what they contain

- The per-account export dialog explains what each choice includes. The two that include the password show a red warning and an **Export With Password** button.
- History-only is the default because it contains nothing that identifies the account.

### Search skips secrets

- Hidden fields and notes aren't searched. Otherwise someone at an unlocked vault could type a guess and see whether it matched, confirming a password or PIN without ever seeing it.
- Notes are left out because that's where people tend to keep PINs and recovery codes.

### Needs Attention limits depend on the category

- A credit card balance goes out of date in weeks, a loan or investment in months, and a salary in half a year.
- Websites, secure notes, and custom categories aren't flagged by default. Passwords and notes don't go out of date by sitting there, and flagging them would fill the tab with things that need nothing.

### Lapsed dates aren't shown in red

- A one-time due date more than 60 days past almost always means the record wasn't updated, not that a bill is still waiting.
- Red is kept for bills the owner can still pay.

### Closed accounts keep their pin

- Paying off a loan or closing a card doesn't mean the owner stops wanting it close at hand.

### The dashboard compares with a table, not cards

- **Current Assets & Liabilities** is a table for each side, then a **Net Worth** row. The sides are kept apart because they have opposite signs, so a share of one total holding both would mean nothing.
- A negative balance is listed and counted in its side's total, so the 2 totals always come to the **Net Worth** row. It gets no share, since a negative amount has no honest share of a total.
- The shares are the same ones the category cards show, so the table and the cards never disagree.
- Every trend card's chart is the same size, the **Net Worth** card's included. That works for reading one trend but is misleading for comparing two, so the table does the comparing.
- Rows and cards are sorted largest first, so the categories that move the total are read first.
- Each category can unfold to list the open accounts its amount is the sum of. They start folded, so the table still compares categories at a glance. An account's share is of its category, not of the side, and is worked out over the positive balances only, as the category shares are, so an overdrawn account can't push the others past 100%.
- This section used to be a bar for each side. With only a few categories per side, a share column reads just as quickly, and a bar can't draw a negative balance, so its totals didn't always add up to net worth.

### Property is a built-in category

- Property holds what a home, vehicle, or piece of land is worth, as an **Estimated Value**. It counts as an asset in every dashboard total.
- Before it existed, Loans already had **Mortgage** and **Auto** types, so net worth subtracted the debt and had nowhere to add back the house or car it paid for. Anyone with a mortgage read as poorer than they were.
- It's built in because custom categories have no balance field, so there was no honest place to enter a home's value.
- It goes stale after 6 months, then red after a year. Nobody revalues a house monthly.
- It has the same login and website fields as the other categories, for a county assessor or vehicle registration page.
- Linking a property to the loan against it was considered and left for later. The two already net out in the totals.

### Each dashboard range includes its starting month

- A range shows its months plus the month it's measured from. **1Y** runs from this month last year to this month, and **1M** is last month and this month.
- Without that extra month, "vs last year" would really compare with 11 months ago, and **1M** would be a single point with no line to draw.
- The change line runs from the first point on the chart to the last, so the figure always matches the line.
- When the history is shorter than the range, the change line names the month it starts in ("since Feb 2026"), so 8 months of history is never called a year.
- One set of range buttons, beside the **Trends** heading, sets the range for every card under it. A click redraws that section from figures already loaded, without reading the vault again.
- An account counts from the month of its first balance, so starting to track an account shows up as growth. Counting only accounts with a balance at both ends of the range was considered and left for later.

### Balance history uses the same ranges

- An account's **Balance History** has the same range buttons and change line as the dashboard's **Trends**. The ranges, their wording, and the colors come from one module, `ui/ranges.py`, so the two can't drift apart.
- History entries are dated to the day and filed whenever someone records a figure, so a range's first day rarely has one. The range opens on the last entry on or before that day, which is what the account held then. It's the dashboard's carry-forward, applied to one account's entries.
- The chart starts on that same entry, so the change line always matches the line drawn.
- When nothing was filed during the range, the figure held for all of it, so the change line reads "■ $0.00 vs last month" instead of showing nothing.
- The range filters the chart, not the table. The table is where entries are edited and deleted, and hiding some would make it easy to miss one, or to think an import skipped something.
- The chosen range is kept for every account until the app closes, so stepping between accounts compares the same window. It isn't saved.
- A falling figure is green for credit cards, loans, and utilities. Utilities isn't a debt and stays out of net worth, but a smaller bill is still good news.

### Dashboard text is selectable, and a click still opens

- Every figure and name on the dashboard can be selected and copied, like a field in the detail view.
- Selecting text takes the mouse, so a card would never see a click on its own title. Each card watches its labels instead: a release that selected nothing counts as a click. A plain click opens the category, and a drag selects.
- Table cells select by dragging, Shift-click, or Ctrl-click. A plain click on a row still opens it. With Shift or Ctrl held, a click only selects.
- Copied cells are tab-separated rows, with an empty cell for any gap in the selection, so they paste into a spreadsheet in the same columns.
- The copy is plain text and isn't cleared later, the same as text selected by hand anywhere else in the app. These are balances, not passwords.
- Selected cells get the raised background and an accent border, the way a selected account row does. An accent fill would hide category names, which are already drawn in the accent color.

### Investing vs. income is yearly, and current only

- The section compares what goes into Investments accounts with what Income accounts pay, as yearly totals. Income already stores yearly pay, and a year holds a whole number of every period. A month doesn't: some months have 3 paydays every 2 weeks, others have 2.
- A contribution is an amount plus a period: weekly, every 2 weeks, monthly, or yearly. Percent-of-pay plans and employer match were considered and left out to keep it simple. A 401k is entered as the dollar amount from the pay stub.
- It shows current figures only. A monthly trend would need a dated history of contribution settings, which was considered and left for later. Closed accounts are left out on both sides, because with no history, a closed job's pay would otherwise count as income for good.
- Nothing is guessed. An account with no contribution amount, or zero, isn't being paid into and isn't listed. An amount with no period is listed as "needs a period" but not counted. Every account saved before the period field existed starts that way.
- The period field has no default, for the same reason. A new account would otherwise arrive with a period nobody picked, and it would be counted as if someone had.
- The period is read exactly as stored, with no aliases like "biweekly". The edit form shows anything outside its options as blank, so counting it would mean counting a period the form says isn't set.

### Social Security and pensions are Income records

- They go in Income, with no tab of their own, because they're income in retirement. **Income Type** says which: Pay, Social Security, Pension, or Other. Blank counts as Pay, so records from before the field keep counting as they did.
- A Social Security or Pension record has a **Monthly Benefit** in place of Annual Pay, because statements give a monthly figure. For Social Security still to come, it's the benefit at 67. Once it's being paid, it's what's paid.
- **Whose** says which of the household it belongs to, for the retirement planner. Blank counts as yours.
- It isn't income until it's paid, so the dashboard counts it only while **Receiving Now** is ticked, as 12 monthly benefits. Otherwise a benefit years away would raise income today and lower the investing rate.
- With no Annual Pay, the record has no balance, so it has no Quick Update, Balance History, or figure on its list row. A job made into Social Security loses its pay on save but keeps the pay's history.

### Error logs never include the message

- Under `pythonw`, an unhandled error would otherwise just disappear, so it's shown on screen and added to `errors.log`.
- The log records only the error type and where it happened. A message can quote a value (`float('hunter2')` puts the password in its error), and the log is plain text outside the vault.
- stderr gets the same redacted text. It looks like a terminal, but when the app starts from a desktop shortcut on Linux or macOS it goes to the system journal or `~/.xsession-errors`, which is also plain text outside the vault.
- Setting `ACCOUNTVAULT_DEBUG=1` prints the full traceback, message included, to stderr for development. Only the exact value `1` counts, and the log stays redacted either way.

---

## 6. Retirement planner

- The planner is its math, in `core/retirement.py`, the record of how Investments accounts are taxed (see **A Both account keeps two amounts that add up to its balance**), and a **Retirement** tab with six pages: **Plan**, **Projection**, **What-if**, **Scenarios**, **Withdrawals & taxes**, and **Your details**.

### The plan is stored encrypted in the vault

- One `app_config` row, `retirement_plan`, holds the plan as JSON, encrypted with the vault key and tied to its row, like the warning settings.
- Why: it holds birth dates, spending, Social Security, and pensions. Keeping it in the vault means a backup carries it and another vault never sees it.
- A password change and the login re-key re-encrypt it with the other encrypted rows. Left out, the plan would be lost.
- Reading it while the vault is locked raises an error instead of returning "no plan". A caller that took that for an empty plan would save over the real one.
- A value that doesn't validate is dropped on its own. A newer version might allow a value this one doesn't, and that should cost one field, not the whole plan.

### Ages are stored as birth dates

- The plan keeps a birth year and month and works out ages each time the tab opens. An age saved this year would be wrong next year.
- Only the month is known, so a birthday counts as reached from the 1st of its month.

### Savings come from the accounts every time

- Only figures typed over the accounts' figures are stored. Everything else is worked out when the tab opens, so the plan follows every balance update. Social Security and pensions come from Income the same way (see **Social Security shows every claim age**).
- 401(k), 403(b), 457(b), IRA, SEP IRA, and SIMPLE IRA accounts go by their tax treatment. Roth IRAs are Roth, HSAs are HSA, and brokerage accounts are taxable. Checking, savings, and money market accounts are taxable cash, because that money is there to spend in retirement too.
- Nothing is guessed without saying so. An account with no tax treatment counts as Traditional and is marked "assumed". A Both account's amounts scaled to fit a balance that Balance History moved are marked "estimated". Accounts the plan can't place are listed and not counted.
- A contribution with no period isn't counted, as on the dashboard (see **Investing vs. income is yearly, and current only**).

### The plan waits for the details only the user knows

- Birth dates and spending have no defaults, because any guess would decide the answer. Until they're entered, **Plan** shows Runway's sample household with a banner saying what's missing.
- Everything else starts from a default: retire at 65, a partner the same year as you, no pay for either of you once the other retires, plan to 95, Runway's market assumptions, no Social Security, no pensions, no Roth conversions, and $6,500 a year each for health costs from 65 (see **Health costs are on top of spending**).
- The sample is a preview. Viewing it never changes the saved plan.

### Your details saves as you go

- Each field is saved when it's committed, by Enter or by leaving the field. There's no Save button: there's one plan, and any figure is easy to put back.
- An entry that can't be read shows an error under its field and isn't saved.
- On lock, the plan is dropped from the window before anything hides, because a field losing focus commits, and there must be no plan left for it to save into.

### Social Security shows every claim age

- There's one figure per person, the monthly benefit at 67 from their statement. That's the figure the statement gives, and every other age follows from it.
- It comes from the Social Security records in Income (see **Social Security and pensions are Income records**), each counted for whoever it belongs to. A figure typed in Your details is used instead, until it's cleared. Pensions work the same way.
- Typed figures stay with the details, not with the savings overrides. That's where they were stored before the records could supply them, so existing plans keep their figures with no change.
- A benefit already being received is paid as it is. The planner scales a benefit at 67 by the claim age, so a received one counts as whatever amount at 67 scales back to it. The claim age then has to be the age it was claimed at.
- Under it, a table lists each claim age from 62 to 70, its share of the age-67 benefit, and what that comes to a month. The row for the chosen claim age is bold.
- The shares come from the same table the planner uses, so the page and the results can't disagree. They're the rates for anyone born in 1960 or later. Claim ages are whole years, with no monthly steps.

### The plan is simulated when a result page is shown

- A simulation takes about a third of a second, and Your details saves on every field. So the plan is only simulated when **Plan**, **Projection**, or **Withdrawals & taxes** opens and something has changed since.

### The withdrawals chart has four series

- Social Security, pensions, a partner's pay, and part-time work share one color as income, and Roth and HSA share one. Taxable and 401(k) / IRA have one each. They stack from the baseline in the order money is drawn.
- Why: the colors come from a palette checked for color-blind separation, and a stack only promises that neighbors can be told apart. A year with nothing from one source lets the sources on either side of it touch. As its own series, part-time work could sit on almost any other, and no color for it could be told apart from all of them.
- With four, one weak pair is left: Taxable against Roth and HSA. They only touch for a household with no 401(k) or IRA money, and the gap between segments, the legend, and the table carry them there.
- The table keeps Social Security, pensions, a partner's pay, and part-time work in separate columns, so nothing is lost. The pension and partner's pay columns are shown only for a plan with some.
- Each table column is as wide as its contents, and any room left over is shared out. A table too wide for the window scrolls sideways instead of cutting figures short.
  - Why: with columns all one width, the eleventh column (pensions) clipped the headers at a normal window size.
- Each source keeps its color on **Projection**'s account mix, and the colors are defined once in `theme.py` with the checks they passed.
- The **What-if** and **Scenarios** line charts use the first five slots in order, the plan first. Lines only promise that neighbors can be told apart, and those five pass that. A legend and a table name every line.

### Money left isn't compared when the money runs out

- The effect of conversions is measured as the money left at the end of the plan, after tax. On a path that runs out there's nothing left either way, so the difference would read +$0. The page says when the money runs out with and without conversions instead.
- The chart and table stop at the year the money runs out. Rows of $0 after it would read as spending that stopped.

### What-if leaves the plan alone until Apply

- The sliders move a copy of the plan. **Apply to my plan** writes only the settings that were moved.
- A plan value outside a slider's range (spending above $250,000, say) keeps its own value until that slider is moved. Snapping it to the slider's end would make the what-if differ from the plan before anyone touched it.
- A value typed into a slider's box works the same way: kept as typed if the plan can run on it, with the slider at its nearest end. A value the plan can't run on puts the box back.
- Dragging updates the figure as it goes and simulates on release, since a simulation takes a few tenths of a second.

- Each extra spending item gets its own slider, for its yearly amount only. Its start and length stay as entered, so the page stays one row per item.

### Scenarios keep only what they changed

- A saved scenario keeps the What-if settings that differ from the plan, and nothing else. Runway kept a copy of every input, savings included.
- Why: savings come from the accounts and change with every balance update. A full copy would compare a scenario made last spring against last spring's balances. Keeping only the changes means a scenario follows everything else in the plan.
- There's room for 4, as many as **Scenarios** can compare beside the plan. A fifth is refused with a message saying to delete one.
- An extra spending amount is kept by the item's id, so renaming the item doesn't lose it. Deleting the item drops its amount from every scenario.
- **Make this my plan** writes a scenario's settings into the plan. A partner claims Social Security at the same age, as the What-if slider has it.
- **Scenarios** always compares against the user's own plan, so it doesn't follow the sample household. Until the plan can run, it says what's missing.

### Roth conversions are set on Withdrawals & taxes

- That's where Runway put the setting, beside the figures it changes. It's saved to the plan like any other detail.
- Your details keeps its own copy of the plan, so it takes the new setting too. Otherwise its next save would put the old one back.
- The sample household's setting is shown but can't be changed.

### The math is ported from Runway

- `core/retirement.py` comes from Runway, a standalone retirement planner page written in JavaScript. It covers federal tax, the order accounts are drawn from, Roth conversions, required distributions, and 1,000 simulated markets.
- It follows Runway down to the order of each calculation, so a household gets the same answer from both. A test pins Runway's sample household to the figures Runway shows for it.
- It's in `core/` because it's pure math. It takes numbers and returns numbers, with no Qt, database, or vault involved.

### The simulated markets are seeded

- Each of the 1,000 markets has a fixed seed, so every plan runs on the same markets every time.
- Why: comparing two plans on different random markets would let luck decide which one looks better.

### Fixes to Runway

- Runway drew 70 years of market returns. A longer plan, such as someone 30 planning to 105, ran past them. Its balances stopped being numbers, and a balance that isn't a number never runs out, so those markets counted as lasting.
  - The markets now run as long as the plan. Their first 70 years are the same draws Runway used, so every plan Runway could run still gets the same answer.
- Runway had no input for HSA contributions. They're added each year until retirement, like the other contributions.
- Runway made room for a fifth scenario by deleting the oldest without a word. A fifth is now refused with a message (see **Scenarios keep only what they changed**).
- Runway had no input for pensions (see **A pension is ordinary income for life**).
- Runway had no health costs after 65 (see **Health costs are on top of spending**).
- Runway retired a couple together. Here each person retires at their own age, with their own pay and contributions (see **Each of you retires on your own**).
  - Runway's sample household now gets "work 10 more years" instead of "work 3 more years". Working on alone while your partner is retired brings in only the pay entered, and the sample has none. Its other moves and its odds still match.

### A pension is ordinary income for life

- **Your details** takes a monthly amount and a start age for you and for a partner. Each person's pension starts at their own age and pays until the plan ends.
- It's taxed as ordinary income, like part-time work. So it also raises the tax on Social Security and leaves less room for Roth conversions.
- **Rises with inflation** keeps a pension's value in today's dollars. Unticked, the payment is a fixed dollar amount, so it loses the inflation rate every year, counted from today.
  - Why from today: a statement's "$2,000 a month at 65" is a fixed dollar figure. By the time it starts, it buys less than $2,000 does now.
- Income counts only in retired years, as Social Security does, so a pension that starts while still working is counted from retirement. Before then, what comes in is what the household saves.
- The sample household has no pension, so it still matches Runway.
- Not yet: a Pension account type that fills these in, survivor benefits, a lump sum instead of payments, and pension start age as a What-if slider.

### Each of you retires on your own

- The planner doesn't assume a couple retire together, or at any set age. **Your details** takes **Retire at** and **Partner retires at**, each in that person's own age.
  - **Partner retires at** may be left blank. It then means the year you retire, so a plan saved before this keeps its answer. The field says **Same year as you**.
- The household saves while both work, and starts drawing on its savings the first year either of you is retired.
- The one still working is paid **Your pay per year** or **Partner's pay per year**, until their own retirement. The pay is before tax, and taxed as ordinary income, like part-time work.
- Each person's contributions go in until their own retirement, paid out of their pay once the household is drawing. With no pay entered, none go in then.
  - So working on while your partner is retired adds little unless **Your pay** is entered. **Plan** and **Your details** say so in that case.
  - The pay counted is what's left after contributions. That's right for pre-tax contributions and taxes Roth and taxable ones a little low.
- Whose contributions are whose comes from the **Whose** field on each Investments account (see **Accounts**). Blank reads as yours. A plan for one counts a partner's account as yours.
  - No Joint option: mark a shared account for whoever pays into it.
- Each person's health costs count from their own retirement. Part-time work and extra spending count from yours.
- **What-if** has a slider for each person's retirement age. The partner's shows only for a plan for two.
- **Next best moves** looks at working longer and retiring earlier for each person on their own, and **Try it** moves that person's slider.
- **Projection** marks the partner's retirement when it's in a different year from yours. **Withdrawals & taxes** adds **Your pay** and **Partner's pay** columns only for a plan with some.
- The sample household retires together with no pay, so its odds still match Runway. Its "work longer" move doesn't (see **Fixes to Runway**).
- Not yet: a Joint owner for accounts, and balances split by person.

### Health costs are on top of spending

- **Yearly spending** leaves health out. Health costs have their own card in **Your details** and are added to spending each retired year.
- Each person has two figures: **health premiums before 65**, and **health costs from 65** (Medicare Part B, Part D, and a supplement). Each person switches from one to the other at their own 65.
  - Why per person: a couple are often different ages. A younger partner still needs coverage for years after the older one is on Medicare.
- Premiums before 65 start at $0. Health costs from 65 start at $6,500 a year each, a rough figure for those three, shown in the field until a figure is typed over it.
  - Why a figure and not zero: leaving them out makes every plan look better than it is.
- Health costs rise with inflation, like spending, so in today's dollars they stay put.
- The **Year by year** table shows spending and health together, as **Spending + health**.
- The sample household still matches Runway. Runway's one premium is for the household, so the sample puts it all on you, with no partner premium and no health costs from 65.
- A plan saved before the split keeps its premium as yours. The partner's premium starts at $0, so the plan's answer doesn't change until it's entered.
- Not yet: health costs rising faster than inflation, and Medicare surcharges (IRMAA) on higher incomes.

### Extra spending counts from retiring

- An item is so much a year, starting so many years after you retire, for so many years. A purchase is 1 year. It's added to that year's spending, like health costs.
- Why years after retiring and not an age: an item moves with the retirement age, so trying a later retirement in **What-if** moves the trip too.
  - It also means an item never falls before retiring, when the plan doesn't take spending from savings.
- Years past the end of the plan aren't counted, and neither are they in the totals shown on **Plan** and **Scenarios**.
- The items are a list inside the plan's details, in the same encrypted `retirement_plan` row, so re-keying needed no change. A plan saved before them has none.
- In the engine's inputs an item is a tuple, so the inputs stay hashable for the scenario results cache.
- There's room for 8 items, to keep **What-if** to a manageable number of rows.
- A bad item is dropped on its own when the plan is read, as a bad detail is.
- **Withdrawals & taxes** counts extra spending as spending: the bars and the **Spending + health** column include it.
- Not yet: moving an item's start or length in **What-if**, chart markers where items start, items tied to the partner's retirement, items before retiring paid from savings, and costs that rise faster than inflation.

### Everything is in today's dollars

- Returns are after inflation, and the 2026 tax brackets are used for every year of the plan. The IRS raises the brackets with inflation each year, so in today's dollars they stay about the same.
- The tax tables still need replacing each year when the IRS publishes new figures.

### What it doesn't model

- Federal income tax is the only tax. State taxes, Medicare surcharges, and ACA subsidies aren't included.
- Required distributions start at 75, the age for anyone born in 1960 or later.
- A couple files jointly in every year of the plan.
- Both people live to the end of the plan, so a pension or Social Security never drops to a survivor's amount.
- Social Security, pensions, required distributions, and market returns are simplified.

### Next best moves are found one simulation at a time

- Each suggestion, such as "work 3 more years", comes from simulating the plan again with that change, up to about 30 times. That's about 2 seconds at worst.
- The search pauses after every simulation. The Retirement tab runs one step per turn of the event loop, so the window stays responsive while **Plan** says it's working out the options.
- Any change to the plan, and any lock, stops the search. The next time **Plan** is drawn, it starts again.
- **Try it** opens **What-if** with the move already made, so the suggestion can be checked before it's applied.

---

## 7. Platforms

- Windows and Linux are the targets. macOS works because Qt supports it. `requirements.lock` has hashes for Windows and Linux on x86-64 and macOS on Apple silicon. Intel Macs aren't covered, because `cryptography` publishes no wheel for them, so they install from `requirements.txt` without the check.
- The launchers always run the app from the project's `.venv`, never a system Python that happens to have the packages, because only the `.venv` was installed with `--require-hashes`. They keep a copy of the lock the `.venv` was installed from and offer to reinstall when it changes, so the `.venv` can't drift from the lock.
- When no usable Python is found, the launchers offer to install one, and only after a yes. They use the platform's own installer (`winget` with the python.org package on Windows, Homebrew, `apt-get`, `dnf`, or `pacman` elsewhere) rather than downloading an installer themselves, so the download is checked by that tool, not by a script.
- On Windows, "usable" means a version the lock covers (3.11 through 3.15). Outside that range, the hash-checked install would fail anyway. The Windows launcher installs per-user, so no administrator prompt is needed and the install lands at a known path. It then uses that path directly, because the open window's PATH doesn't yet include the new Python.
- The Windows launcher sets `NoDefaultCurrentDirectoryInExePath`. Without it, cmd looks in the current folder (the app's folder) for `python.exe` before PATH, so a `python.exe` dropped there would run instead.
- CI only runs on Windows, the one platform where `restrict_to_owner` does real work through an access list.
- Paths that belong to the code (the icon, the README, and this document) are anchored to the source file, not the working directory, because a shortcut, `AccountVault_Windows.bat`, and a terminal all start in different places.
- Paths that belong to the user are kept away from the code:
  - The code folder isn't restricted to the owner, and someone who can change `core/encryption.py` doesn't need to read the vault at all.
  - Under `Program Files`, Windows can silently redirect the vault into a hidden per-user copy that neither the user nor their backup tools can find.
  - The data folder is `%LOCALAPPDATA%\AccountVault` on Windows and `$XDG_DATA_HOME/AccountVault` (usually `~/.local/share/AccountVault`) elsewhere. The vault itself goes wherever the user puts it.
- Platform-specific code covers file permissions (`chmod` or `icacls`), the Windows taskbar identity, locking when Windows locks, and the removable-drive warning for backup folders.
  - `icacls` is run by its full path, so a fake `icacls.exe` in the working directory is never picked up.
  - Each is checked with `os.name` and is best effort. A vault on a drive that can't do owner-only permissions must still open.

---

## 8. Testing

- Tests focus on `services/` and the parts of `core/` where a bug could destroy data for good: `auth.py` (throttling, password changes, format upgrades), `encryption.py`, and `preferences.py` (the backup seal and warning settings).
- Most of the Qt interface isn't tested, because its bugs are cosmetic and widget tests cost more than they catch.
- The exception is any widget that handles stored values or secrets directly. The account form once destroyed stored fields on save, and locking the vault once left a copied password on the clipboard. `tests/test_ui.py` states this rule at the top.
- `tests/test_layers.py` checks structure instead of behavior (see **Layers**).
- `core/retirement.py` is tested against tax figures worked out by hand, and its results for Runway's sample household are pinned (see **The math is ported from Runway**).
- The stored retirement plan gets the warning settings' tests: encrypted on disk, a tampered row, a locked vault, a password change, and a work-factor upgrade. **Your details** is tested like the account form, since it writes stored values.
- A fixture runs key derivation at its minimum settings, since the real settings take about a second each and most tests don't depend on them. `test_rotation.py` checks the real settings directly.
- CI runs the tests on every push and pull request under Python 3.11 and 3.15, the oldest and newest versions the lock supports. It installs with `--require-hashes`, the same check `AccountVault_Windows.bat` makes.
