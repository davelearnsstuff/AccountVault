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

- There are three runtime dependencies: `PySide6`, `cryptography`, and `argon2-cffi`.
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
                     CSV import/export
  core/              No Qt, and nothing above it is imported
    auth.py          Master password, login throttling, session lock,
                     vault format upgrades and re-keying
    encryption.py    Argon2id key derivation, AES-256-GCM envelope
    database.py      Schema, connections, file permissions
    preferences.py   User settings, view state, the backup seal,
                     warning settings
    categories.py    Per-category field schemas, warning rules, formatting
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

### Each field is tied to its column

- Each value is encrypted with its table and column as associated data, such as `accounts.password` or `balance_history.balance`.
- Why: without it, anyone who can edit the file could move a note's encrypted value into the password column, and the app would show and copy the note as the password.
- Limit: fields aren't tied to their row, so swapping two accounts' passwords still decrypts.

### The key and the password check use the same Argon2id settings

- A stolen database contains both the password hash and the salt, and an attacker can go after whichever is cheaper to crack.
- At the argon2-cffi defaults, the hash was about 4 times cheaper to attack than the key derivation. That quietly capped how strong the vault really was.

### Argon2id settings are saved in the vault, so they can be raised

- The password hash stores its own settings and is re-hashed at the next login when they change.
- The key's settings are saved in `kdf_params`. Unlock uses the saved settings. If they're older than the current ones, `migrate_vault_format` re-keys the vault with a new salt during that same login, the only time the password is available.
- Saved settings are read before anything is verified, so values outside sane limits are ignored. Otherwise a crafted vault could ask for 64 GiB of memory and freeze the computer that opens it.
- `LEGACY_KDF_PARAMS` reproduces the key for vaults made before settings were saved. **Never edit it** to match the current settings.

### Format upgrades happen at login

- `vault_format` 0 has no column binding. Format 1 adds column binding, saved Argon2id settings, and a key check.
- An older vault is upgraded before any field is read. This version reads every field with its column binding, so an un-upgraded vault would look completely unreadable, and the first save would write that back.
- A single field that won't decrypt is skipped, since it's already lost. If *nothing* decrypts, the key is wrong, so the login fails instead of rewriting a vault that could still be recovered.
- The file is compacted once after the upgrade, because `secure_delete` only scrubs data deleted after it was turned on.

### A key check catches a key that opens nothing

- The password hash and the salt are separate rows. If they get out of sync (a half-restored backup, or a forged hash), the password is accepted but the key opens nothing. Every field shows as unreadable, and the first save overwrites the real value.
- A known value encrypted with the vault key is checked at every unlock, which turns that into a refused login.

### Changing the password is all or nothing

- Every field is re-encrypted with the new key. If any field won't decrypt, the change is canceled and the vault is left untouched, because a vault split across two passwords can't be fully opened with either.
- This is the opposite of the login upgrade, on purpose. A password change can wait until the field is fixed; a login can't.
- Both re-key paths also re-encrypt the encrypted `app_config` rows (the warning settings and the backup seal) in the same transaction. Otherwise those rows would be left under a key nothing can produce anymore.

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

### Hidden fields and the clipboard

- The copy button on a hidden field copies the real value without showing it, so a card number never has to appear on screen.
- Copied secrets are cleared after a set time, but only if the clipboard still holds what was copied.
- Only the copy buttons and copy shortcuts work this way. Text selected and copied by hand never passes through the app, so the app can't clear it.

### Unreadable fields show a placeholder

- Showing the raw encrypted text would make it look like a real value. It would display as the password, export as the password, and be saved back as the password.

### Stored values are shown as plain text

- Qt treats text that looks like HTML as formatting, so a password like `xK9<b>mq2` would show up bold and missing its tag.
- Stored values go through `plain_label`, which forces plain text.

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

### Error logs never include the message

- Under `pythonw`, an unhandled error would otherwise just disappear, so it's shown on screen and added to `errors.log`.
- The log records only the error type and where it happened. A message can quote a value (`float('hunter2')` puts the password in its error), and the log is plain text outside the vault.

---

## 6. Platforms

- Windows and Linux are the targets. macOS works because Qt supports it, but `requirements.lock` only has hashes for Windows and Linux x86-64, so macOS installs come from `requirements.txt` without that check.
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

## 7. Testing

- Tests focus on `services/` and the parts of `core/` where a bug could destroy data for good: `auth.py` (throttling, password changes, format upgrades), `encryption.py`, and `preferences.py` (the backup seal and warning settings).
- Most of the Qt interface isn't tested, because its bugs are cosmetic and widget tests cost more than they catch.
- The exception is any widget that handles stored values or secrets directly. The account form once destroyed stored fields on save, and locking the vault once left a copied password on the clipboard. `tests/test_ui.py` states this rule at the top.
- `tests/test_layers.py` checks structure instead of behavior (see **Layers**).
- A fixture runs key derivation at its minimum settings, since the real settings take about a second each and most tests don't depend on them. `test_rotation.py` checks the real settings directly.
- CI runs the tests on every push and pull request under Python 3.11 and 3.14, the oldest and newest versions the lock supports. It installs with `--require-hashes`, the same check `AccountVault_Windows.bat` makes.
