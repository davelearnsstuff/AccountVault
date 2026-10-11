# AccountVault

AccountVault is a password manager and money tracker that runs entirely on your own computer. There's no cloud, no sync, and no account to sign up for, and your data never leaves your machine.

It's a regular desktop app built with PySide6 (Qt). There's no web server or browser involved.

**TLDR:** AccountVault is a password manager and money tracker that keeps everything in one encrypted file on your own computer. It makes no network connections.

- Every field is encrypted, account names included, and one master password of at least 15 characters opens the vault. If you lose that password, nothing can recover your data.
- It stores logins, cards, bank and loan details, and secure notes. It also tracks balances, due dates, and net worth over time.
- The vault is a single `.vault` file you can keep anywhere. A backup is a copy of that file and opens with the same password.
- To start, run `AccountVault_Windows.bat` on Windows or `./AccountVault_Mac_Linux.sh` on Linux or macOS. To look around first, click **Create Demo Vault…** on the first screen.

This README has two parts. Everything before **For developers** is about using the app. **For developers** covers the security design, where files are kept, the database, tests, and dependencies.

## Features

### Accounts

- Built-in categories are Credit Cards, Banking, Loans, Investments, Property, Income, Utilities, Websites, and Secure Notes. You can also type in a category of your own.
- Each category has its own fields. Credit cards have a credit limit, minimum payment, due date, and interest rate. Loans add a lender, payoff date, and monthly payment. Investments have a contribution amount and how often you make it. Property (a home, vehicle, or land) tracks an estimated value instead of a balance. Income tracks yearly pay.
- Sensitive fields stay hidden until you click **Show**. These are passwords, card numbers, account and routing numbers, security questions and answers, and the contents of secure notes.
- Copy buttons sit next to usernames, passwords, websites, and card, account, and routing numbers. On a hidden field, the button copies the value without showing it on screen. Only values copied with these buttons or the copy shortcuts (see **Keyboard shortcuts**) are cleared from the clipboard automatically. Text you select and copy by hand isn't.
- Every password field has a **Generate** button. You choose the length and which kinds of characters to include.
- **Quick Update** changes the balance and the due date without opening the full form. Use its **As Of** date to file a figure under the day your statement covers. An older figure only becomes the current balance if nothing newer is on file.
- **No Change** marks an account as checked. It sets the Last Updated date to today and saves the current balance to the history.
- The arrows next to an account's name step to the previous or next account in the list.
- Click the star next to an account's name to pin it. A **Favorites** tab shows up while anything is pinned, and pinned accounts show a star in every list. Closed accounts keep their pin.
- You can mark an account as closed. Closed accounts move to the **Closed** tab and aren't counted in the dashboard totals.
- Only closed accounts can be deleted. After you delete one, you have 10 seconds to undo it from the message that appears. After that, only a backup can bring it back.
- Shift-click or Ctrl-click to select several accounts, then close them, reopen them, or move them to another category all at once.

### Due dates

- A due date can repeat weekly, monthly, quarterly, or annually, and the app figures out the next one for you. New accounts in categories that have a due date start out monthly. Older accounts stay **One-off** (a one-time date) until you change them.
- Quick Update's **+1** button moves the date by the account's own cycle. A quarterly bill moves 3 months in one click, and an annual one moves a year.
- Each due date is worked out from the original date, so a bill due on the 31st shows as the 28th in February and goes back to the 31st in March.

### Finding things

- Search looks across every category at once and shows which field matched, including category names. It doesn't search hidden fields or notes, so nobody can use it to check a guess about a password or PIN.
- Sort by name, due date, or last updated, in either direction. The app remembers your choice.
- A **Needs Attention** tab appears when something is due within 14 days, is past due, or hasn't been updated in a while. It's hidden when there's nothing in it.
- After a Quick Update or **No Change**, the account stays in Needs Attention so you aren't moved off it. Click the refresh button next to the list title to remove the accounts you've dealt with. The account you're viewing stays open if it still needs attention.
- How long counts as "a while" depends on the category: 3 weeks for credit cards, bank accounts, and utilities, 2 months for loans and investments, and 6 months for property and income. Websites, secure notes, and custom categories are never flagged unless you turn it on for them.
- A one-time due date that's more than 60 days past is shown as "lapsed" instead of in red. It's probably just out of date, not a missed bill.
- You can change all of these limits under **Manage > Warnings…**. You can set when due dates turn orange and red, when a one-time date counts as lapsed, and, for each category, whether due dates and updates are checked and after how many days. These settings are encrypted, because they include the names of your custom categories.

### Money tracking

- Every time an account's balance changes, a dated entry goes into its balance history. You can also add, edit, and delete entries yourself.
- The newest entry is the account's current balance. An entry dated on or after all the others updates the balance and moves the Last Updated date forward. An older entry just fills in the past. If you delete a wrong newest entry, the balance goes back to the one before it.
- The history table shows the change from each entry to the next (`+$250.50`, `-$150.50`). The oldest entry and a second entry on the same day show a dash.
- The range buttons beside **Balance History** (**1M**, **3M**, **6M**, **1Y**, **2Y**, and **All**) set how much of the history the chart shows. The table always lists every entry.
  - A change line above the chart shows how much the figure moved over the range, for example "vs last year", worded and colored the same way as on the dashboard. For credit cards, loans, and utilities, a falling figure is green.
  - The range is measured from what the account held on the range's first day: the last entry on or before that day. If there's no entry that old, the line says "since" the first entry's date instead, for example "since Mar 4, 2025".
  - The range starts on **1Y**, and the one you pick stays chosen as you move between accounts until you close the app.
- To import past balances, paste two columns from a spreadsheet or choose a CSV file from your bank. Before anything is saved, the app shows which columns it picked as the date and amount, a preview of every entry, and how many rows it skipped and why. Entries the account already has are skipped, so importing the same statement twice doesn't add anything.
- Most date formats work: `2024-01-05`, `01/05/2024`, `Jan 5 2024`, or a full timestamp. Dates with slashes are read month first (so `01/05/2024` is January 5), unless the first number is over 12. A month by itself, like `2024-04`, is saved as the first of that month. Any other date the app can't read for certain is skipped and listed in the report.
- You can export the history as a CSV of dates and amounts, oldest first, with the account's **Export** button (see **Files, backups, and export**). Dates are written as `YYYY-MM-DD`, so the file imports back with the same dates.
- The **Dashboard** tab has 3 sections, in this order: **Current Assets & Liabilities**, **Trends**, and **Investing vs. Income**. Credit cards and loans count as liabilities (money you owe). Utilities, income, websites, and secure notes aren't included in the totals.
- Property counts as an asset, so a mortgage or car loan is set against the home or car it paid for. Without a Property entry, the loan is subtracted from your net worth with nothing to balance it.
- **Current Assets & Liabilities** shows your current balances in two tables, one for assets and one for liabilities. Each lists its categories, largest first, with each category's amount and share and a total. A **Net Worth** row underneath is your assets total minus your liabilities total.
  - Click the arrow beside a category to show the open accounts that make up its amount, each with its balance and its share of the category. Click it again to hide them. Categories start folded, and stay as you left them until you close the app.
  - Click a category's name to open that category, or an account's name to open that account.
  - A category with a negative balance (an overdrawn account, say) is listed and counted in the total, but has no share.
- **Trends** has a card for your net worth first, then one for each category, largest first. Each card shows the current amount, how much it changed, and a chart of it month by month. Category cards also show their share of your assets or liabilities.
- The cards all draw their charts at the same size, so use the **Current Assets & Liabilities** table to compare categories.
- The range buttons beside **Trends** (**1M**, **3M**, **6M**, **1Y**, **2Y**, and **All**) set how much history every card in it shows. They start on **1Y**.
  - A range includes the month it's measured from. **1Y** runs from this month last year to this month, and **1M** shows last month and this month.
  - The change line under each figure follows the range: "vs last month", "vs 3 months ago", "vs 6 months ago", "vs last year", "vs 2 years ago", or "since" your first month for **All**.
  - If your history doesn't go back as far as the range, the change line names the month it starts in instead, for example "since Feb 2026".
- **Investing vs. Income** compares what you put into investments with what you earn, as yearly totals.
  - Every open Income account counts, using its yearly pay.
  - Every open Investments account with a **Contribution Amount** counts. Its **Contribution Period** (weekly, every 2 weeks, monthly, or yearly) turns the amount into a yearly figure. For example, $100 every 2 weeks is $2,600 a year.
  - Income and contributions each get a table, largest first, with a total row and the share of your income going into investments. Click a row to open that account.
  - An amount with no period is listed as "needs a period" and isn't counted until you pick one. Accounts created before the period field existed start out this way. An income with no pay entered is listed but not counted either.
  - It shows the current figures only, with no history. Closed accounts aren't included.
- If a month has no entry, the dashboard uses the last known balance instead of zero.
- Links next to the **Dashboard** title jump to each section. They stay in place while you scroll.
- You can select any text on the dashboard and copy it.
  - On a card, drag across the text to select it. A plain click still opens the category.
  - In a table, drag across cells, or Shift-click or Ctrl-click them, then press Ctrl+C or right-click and choose **Copy**. The cells paste into a spreadsheet as cells. A plain click on a row still opens it.

### Files, backups, and export

- You choose where your vault is saved. You can create a new one anywhere, open another, and switch between them. The app remembers the ones you've opened.
- A backup is a copy of the vault file, so it opens with the same master password. **Manage > Save a Copy…** makes one whenever you want. See **Backups and restoring** for automatic backups.
- **Manage > Export CSV…** saves all your accounts as a spreadsheet. It isn't encrypted, and the menu says so.
- **Manage > Import Accounts…** reads accounts from a CSV file. That can be this app's own export, another password manager's export, or your own spreadsheet. The first row must have column names. Before anything is saved, the app shows which field it matched to each column and which columns it skipped. Accounts already in the vault are skipped, so importing the same file twice doesn't add anything. Either the whole file is imported or none of it is.
- Each account's **Export** button offers 3 choices: everything, the account details only (name, username, password, website, and the category's own fields), or the balance history only. The two choices that include the password show a red warning and an **Export With Password** button. The history-only choice contains nothing that identifies the account, and it's the default. Categories without a balance only offer the details.

### Locking and appearance

- The vault locks after 5, 10, 15, 30, or 60 minutes of inactivity. On Windows it also locks right away when you lock your computer (Win+L, a screen saver that locks, or switching users).
- After locking, the app can close itself after 30 minutes or 1, 2, or 4 hours, or stay open.
- Copied passwords are cleared from the clipboard after 15, 30, 60, or 120 seconds.
- Dark and light themes, 4 text sizes, and a compact or comfortable account list.
- The app remembers the window's size and position and the sidebar's width.

## Keyboard shortcuts

On macOS, use Cmd instead of Ctrl.

| Keys | Action |
|---|---|
| `Ctrl+F` | Go to the search box |
| `Down` | From the search box, move into the results |
| `Esc` | Clear the search |
| `Ctrl+N` | Add an account |
| `Ctrl+E` | Edit the selected account |
| `Ctrl+B` | Copy the selected account's username |
| `Ctrl+Shift+C` | Copy the selected account's password |
| `Ctrl+1` to `Ctrl+9` | Jump to one of the first 9 tabs, starting with Dashboard |
| `Ctrl++` | Make text larger |
| `Ctrl+-` | Make text smaller |
| `Ctrl+,` | Open Settings |
| `Ctrl+L` | Lock the vault |
| `Ctrl+Q` | Quit |

In the **Readme** and **Design Document** windows:

| Keys | Action |
|---|---|
| `Ctrl+F` | Go to the search box |
| `Enter` or `F3` | Next match |
| `Shift+Enter` or `Shift+F3` | Previous match |
| `Esc` | Clear the search, or close the window if the search is empty |

## How your data is protected

- Everything you store is encrypted, including account names, not just passwords. Encryption uses AES-256. The key is made from your master password with Argon2id, which is designed to be slow and memory-hungry, so every password guess takes real effort.
- Your master password is never saved. The key made from it is kept only in memory while the vault is open, and the app lets go of it when the vault locks.
- The app never connects to the internet or opens a network connection.
- After 5 wrong passwords, you have to wait before trying again. The wait starts at 30 seconds and doubles each time, up to 5 minutes. Restarting the app doesn't reset it.
- Only your user account can open the vault file. Other people who log in to the same computer can't, unless they're an administrator.
- Copied passwords are cleared from the clipboard automatically.
- A CSV export isn't encrypted. Anyone who opens one can read everything in it.

There's much more detail in **Security details**, under **For developers**.

### What locking doesn't do

When the vault locks, the app lets go of the key and the decrypted data it was holding, so nothing in the running app can decrypt anything else.

It can't erase that data from your computer's memory right away, though. Python doesn't let a program overwrite data in place, so it stays in memory until the space is reused. Copies also live inside the encryption libraries and Qt's password box, out of the app's reach.

If your computer writes memory to disk (in a page file or a crash dump), some of that data could end up there. Full-disk encryption (BitLocker on Windows, FileVault on macOS, LUKS on Linux) protects against that, and a short auto-lock time limits how long the data stays around.

## Requirements

- Python 3.11 or later. The verified install described in **Installation** supports 3.11 through 3.14.
- pip
- A computer running Windows, macOS, or Linux with a desktop (X11 or Wayland).

## Installation

**Windows:** run `AccountVault_Windows.bat`. If the packages it needs aren't installed, it offers to install them into a `.venv` folder inside the app's folder, without touching your system Python. If Python itself isn't installed, it tells you where to download it and gives you a command that installs it.

**Linux and macOS:**

```bash
git clone https://github.com/davelearnsstuff/AccountVault
cd AccountVault
./AccountVault_Mac_Linux.sh
```

The first time it runs, the script works the same way as the Windows one. If the packages it needs aren't installed, it offers to install them into a `.venv` folder inside the app's folder, without touching your system Python. Run it from a terminal, because it needs to ask before installing.

The script checks every downloaded file against a known fingerprint (`--require-hashes`). Those fingerprints only cover Linux and Windows on x86-64 with Python 3.11 through 3.14. On macOS, ARM Linux, or a newer Python, the script says so and asks before installing the same pinned versions without the check.

On Debian and Ubuntu, creating the `.venv` needs the `python3-venv` package (`sudo apt install python3-venv`). The script tells you if it's missing.

To install by hand instead:

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements/requirements.lock
```

## Running

**Linux and macOS:**
```bash
./AccountVault_Mac_Linux.sh
```

**Windows:**
```bat
AccountVault_Windows.bat
```

**Or directly:**
```bash
python main.py
```

The window opens right away. The first time, you're asked to create a master password of at least 15 characters. You'll need it every time you open the app, and it can't be recovered if you lose it.

To open a specific vault instead of the last one you used, put its path on the command line: `python main.py path/to/my.vault`.

You can read this file inside the app, too: **Manage > Readme**. **Manage > Design Document** opens the design document, which explains why the app works the way it does. Both windows have a search box. Press Ctrl+F, type a word, and press Enter to jump to the next match or Shift+Enter for the previous one.

### Demo vault

A demo vault is full of made-up accounts for a fictional household. It has 31 months of balance history, every kind of repeating due date, out-of-date and lapsed accounts, pinned and closed accounts, and a custom category, so every screen has something on it.

To build one, use either of these. For safety, neither will overwrite a file that already exists, so to replace an old demo, delete it yourself first.

- Click **Create Demo Vault…** on the vault chooser and pick where it goes. The app shows you the password, then asks for it to unlock the new vault.
- Run `python tools/make_demo_vault.py [path]`. With no path, it writes `springfield.vault` in the current folder. It prints the password when it's done. Open the vault with **Manage > Open Vault…** or `python main.py path/to/springfield.vault`.

The balance history and due dates count back from the day it's built, so a fresh one looks current.

## Backups and restoring

A backup is a copy of your vault file. It opens with the same master password, and to restore one, you just open it. There's no import step and no second password to remember.

- To make a backup yourself, use **Manage > Save a Copy…** and save it wherever you like.
- To make backups automatically, turn them on in **Settings** and choose daily, weekly, or monthly. The app checks whether one is due when you unlock the vault, and again whenever the date changes while the vault is open.
- Automatic backups go into a `backups` folder next to the vault unless you choose another folder. They're named after the vault and the date, like `mymoney_backup_20260917.vault`.
- If you choose your own folder, the app asks you to confirm it before the first backup goes there. It also warns you if the folder is on a network share (a path starting with `\\`) or, on Windows, a removable drive. Each backup is a full copy of your vault, and anyone who gets one can try to guess the password on their own computer.
- Old automatic backups are deleted after the time you choose, from 1 month to 2 years (12 months by default), or never. The newest backup is always kept.
- The app never leaves a half-written backup behind. It only deletes old backups right after a new one has been saved and checked, and it never deletes a file it didn't create.
- If the backup settings were changed outside the app, they're reset when the vault opens, and automatic backups stay off until you turn them back on. The app tells you when this happens.

**Changing your master password doesn't change your backups.** Backups made earlier still open only with the old password. Right after you change your password, the app offers to make a fresh backup. Say yes.

**Manage > Move Vault…** moves your vault to another folder, and you keep working from the new location. The original file stays where it was. Delete it yourself once you're sure the move worked.

**Manage > Export CSV…** isn't a backup. It saves an unencrypted spreadsheet, which is handy for moving your data to another app, but anyone who opens the file can read everything in it.

## For developers

The rest of this README is technical. The reasons behind the design are in `docs/AccountVault_Design_Document.md`, which you can also read in the app under **Manage > Design Document**.

### Security details

| Property | Detail |
|---|---|
| Key derivation | Argon2id with 128 MiB of memory, 4 iterations, and 2 lanes. These settings are saved in the vault, so they can be raised later. The vault re-encrypts itself with the new settings at the next login |
| Field encryption | AES-256-GCM, with a new random 96-bit nonce every time a field is encrypted |
| Field binding | Each field is tied to its table and column, so encrypted data moved to a different column or table won't decrypt. This stops someone who can edit the database file from making a note show up as a password. Fields aren't tied to their row, so swapping two accounts' passwords would still decrypt |
| Key storage | In memory only and never written to disk. Released when the vault locks, but not erased from memory (see **What locking doesn't do**) |
| Salt | A 256-bit random value stored in the database |
| Key check | At every unlock, the app decrypts a known test value to confirm the key is right. Without it, a password and salt that don't belong together (from a half-restored backup, for example) would log in, show every field as unreadable, and the first save would write that over the real data |
| Auto-lock | After a set time with no activity, and right away when the Windows session locks. Locking releases the key and drops the decrypted data |
| Network | None. The app opens no sockets and makes no connections |
| Attack surface | One process. Decrypted data never passes to another process |
| Login throttling | 5 failed attempts cause a lockout that starts at 30 seconds and doubles up to 5 minutes. The lockout and the failure count are both saved in the database, so restarting the app resets neither. The lockout is stored as a start time and a length, and the remaining wait is capped, so changing the system clock can't make it longer |
| Clipboard | Copied secrets are cleared automatically, but only if the clipboard still holds what the app copied |
| Database file | Only the owner's account can access it: mode 0600 on Linux and macOS, and an access control list with inherited permissions removed on Windows. It's kept apart from the app's code (see **Where the vault is stored**) |
| Deleted data | SQLite's `secure_delete` overwrites data as it's deleted, so a deleted account doesn't linger in the file. The one-time format upgrade also compacts the file, which clears anything deleted before `secure_delete` was turned on |
| Backup settings | These settings control where copies of the whole vault go and which files can be deleted. Anyone who can edit the vault file could change them without the password, so the app protects them with a seal made with the vault key. If the seal doesn't match, the settings are reset to the defaults, automatic backups are turned off, and the app tells you. A backup folder you choose yourself is confirmed once before the first copy goes there |
| Undo after delete | During the 10 seconds you can undo a delete, the deleted data is held still encrypted, so it's no more exposed than the vault file itself. It's discarded when the vault locks or you switch vaults, because data encrypted with one vault's key must never be restored into another vault |
| Dependencies | Installed with `--require-hashes` against `requirements.lock`, which checks the exact files and not just the version numbers. A tampered `cryptography` or `argon2-cffi` package would run in the same process as the master password |
| Master password | At least 15 characters, with no space at the start or end. It's the only password: a backup is a copy of the vault and opens with the same one |
| CSV export | Values a spreadsheet would run as a formula are made safe before they're written. A per-account export says what it includes before saving and warns when the file will contain the password. The history-only export holds just dates and amounts |
| CSV import | Every column is listed with the field it will be read as before anything is saved, so a wrongly matched column is easy to spot. Columns the app can't match are skipped. Nothing is decrypted or saved until you click **Import**, and the dialog reminds you that the source file is still unencrypted |
| Damaged data | A field that won't decrypt shows a placeholder instead of the raw encrypted text. The placeholder is never saved as a value, whether by a save or by importing a CSV exported from a damaged vault |
| Displayed values | Stored values are always shown as plain text. A value that looks like HTML (`xK9<b>mq2`) is shown exactly as stored |
| Value lengths | A value that's too long for its field is rejected with a message naming the field. It's never cut short without telling you |
| Error log | Unexpected errors are shown on screen and written to `errors.log` next to `vaults.json`, readable only by you. The log records the error type and where it happened, never the error message or any values, because a message can include field contents |

The master password itself is never stored. When you log in, Argon2id turns your password and the stored salt into the AES-256-GCM key, and that key is kept only in memory.

Changing the master password re-encrypts every field with the new key. It's all or nothing. If any field can't be re-encrypted, nothing changes, because a vault that's half on the old password and half on the new one couldn't be fully opened with either.

### Where the vault is stored

You choose. When you create a vault (**Create New Vault…** on the vault chooser, or **Manage > New Vault…**), you pick where the `.vault` file goes. The file itself is always restricted to your account. The folder isn't, because it's your folder, and locking down something like Documents would break every other app that uses it.

If you don't pick a folder, or your vault was created before you could, it goes in the app's data folder: `%LOCALAPPDATA%\AccountVault` on Windows, and `~/.local/share/AccountVault` on Linux and macOS. The app created that folder, so it restricts the folder to your account too.

The app keeps the list of vaults you've opened, including the last one, in `vaults.json` in that same folder. The list holds file paths and nothing else: no counts, no sizes, and nothing about what's inside a vault. To clear the list, click **Clear** above it on the vault chooser. To stop keeping it, right-click the list.

The vault is never kept with the app's code. Nothing locks down the code folder, so a vault stored there would be protected while the code that decrypts it wasn't. Someone who could change `core/encryption.py` wouldn't need to read your vault at all. Keeping them apart also avoids a Windows problem: under `Program Files`, Windows can silently redirect the vault into a hidden copy that neither you nor your backup tools can find.

If you already have `data/accountvault.db`, or a vault in the app's data folder, the app opens it where it is, so upgrading doesn't move anything. To use a different location, such as a USB drive or a second vault kept separate, set the `ACCOUNTVAULT_DB` environment variable or pass a path on the command line.

Install the app itself somewhere other users can't change. The app can't protect your password from someone who has changed its code.

### Why there's no local web server

Older versions ran the interface at `http://localhost:8000`. That needed session tokens, origin checks, and CSP headers to keep other programs and web pages out. Running everything inside one desktop app removes that risk completely, so none of those protections are needed anymore.

### Tests

```bash
python -m pip install --require-hashes -r requirements/requirements.lock -r requirements/requirements-dev.lock
python -m pytest tests/
```

On a platform the lock doesn't cover, install from `requirements/requirements.txt` and `requirements/requirements-dev.txt` instead.

Most tests cover `services/`, `core/auth.py`, `core/encryption.py`, and `core/preferences.py`. Those are where a bug could destroy data that can't be recovered: balance history, vault copies and how long they're kept, the backup seal, the decrypted account cache, password changes and format upgrades, login throttling, the old plaintext migration, and fields that won't decrypt.

Most of `ui/` isn't tested, because bugs there are cosmetic and testing widgets costs more than it catches. The exceptions are the widgets that handle stored values or secrets directly: the account form, which once lost fields it couldn't display, and the clipboard, which once kept a copied password after the vault locked. Those are tested like the services, which is why the test install includes the app's own requirements too.

`tests/test_layers.py` keeps the code layers apart. It fails if anything in `core/` imports Qt, `services/`, or `ui/`, even from inside a function.

The tests run on every push and pull request, on Windows with Python 3.11 and 3.14, the oldest and newest versions the lock covers (see `.github/workflows/tests.yml`). That's what makes Dependabot's version bumps safe to accept: Dependabot opens the pull request, and the tests decide whether it gets merged.

### File structure

```
AccountVault2-PySide/
├── main.py                  # Qt application entry point, error log
├── core/                    # The layer under services/ and ui/; imports no Qt
│   ├── database.py          # SQLite schema, connections, file permissions
│   ├── encryption.py        # Argon2id key derivation + AES-256-GCM
│   ├── auth.py              # Master password, login throttling, session lock
│   ├── preferences.py       # User settings and window view state
│   ├── categories.py        # Field schemas, attention rules, formatting
│   ├── vaultfile.py         # Which vault to open, and the recent list
│   ├── urls.py              # Blocks unsafe link types before a link is opened
│   └── version.py           # The application version
├── services/
│   ├── accounts.py          # Account CRUD, balance history, decrypted cache
│   ├── dashboard.py         # Balance sheet, monthly trends, investing vs. income
│   ├── csv_export.py        # The whole vault as a plaintext CSV
│   ├── account_import.py    # Reads a table of accounts back in
│   ├── backups.py           # Vault copies, the backup schedule, and retention
│   ├── history_io.py        # Reads and writes one account's history as CSV
│   ├── account_export.py    # One account as CSV, with the scope the user picks
│   └── export_io.py         # Shared plaintext-file code: cells, reads, writes
├── ui/
│   ├── main_window.py       # The widget tree, the account form, the dashboard
│   ├── actions.py           # Commands, shortcuts, the Manage menu, appearance
│   ├── vault_session.py     # Which vault, locked or not, and everything on a timer
│   ├── account_list.py      # The list, the tab bar, search, sort, bulk actions
│   ├── auth_screens.py      # Vault chooser, first-launch setup, unlock
│   ├── detail_view.py       # Read-only account detail, per-account export
│   ├── account_form.py      # Add/edit form
│   ├── history_table.py     # Balance history table, chart, and import
│   ├── dashboard_view.py    # Assets and liabilities, net worth, category trends, investing
│   ├── ranges.py            # Range buttons and change lines, shared by both views
│   ├── dialogs.py           # Settings, password, imports, document viewer, about
│   ├── widgets.py           # Charts, toasts, password generator, tabs
│   └── theme.py             # Color palettes and the Qt style sheet
├── tests/                   # The pytest suite (see Tests)
├── assets/
│   ├── accountvault.ico     # Window and taskbar icon
│   └── make_icon.py         # Rebuilds the icon
├── tools/
│   ├── genlock.py           # Rebuilds the hash-pinned lock files
│   └── make_demo_vault.py   # Builds a throwaway vault full of demo data
├── docs/
│   └── AccountVault_Design_Document.md
├── AccountVault_Mac_Linux.sh # Linux/macOS launch script
├── AccountVault_Windows.bat  # Windows launch script, installs if needed
├── requirements/
│   ├── requirements.txt     # Pinned versions
│   ├── requirements.lock    # Pinned files: what actually gets installed
│   ├── requirements-dev.txt
│   └── requirements-dev.lock
└── .github/
    ├── dependabot.yml       # Watches the pins for new releases and CVEs
    └── workflows/tests.yml  # Runs the tests on push and pull request
```

### Database schema

#### accounts

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, auto-increment |
| `category` | TEXT | The category name, for example "Credit Cards", "Banking", or a custom one. Not encrypted |
| `name` | TEXT | Account name, **encrypted** |
| `username` | TEXT | **Encrypted** |
| `password` | TEXT | **Encrypted** |
| `website` | TEXT | **Encrypted** |
| `notes` | TEXT | **Encrypted** |
| `extra_data` | TEXT | The category's own fields (balances, due dates and how they repeat, rates) plus the Favorites pin, stored as JSON and **encrypted** as one value. Keeping the pin here means the file doesn't reveal which accounts you use most |
| `closed` | INTEGER | 0 for open, 1 for closed |
| `date_added` | TEXT | YYYY-MM-DD, set when the account is created. Not shown anywhere in the app |
| `last_updated` | TEXT | YYYY-MM-DD. Set to today by an edit or **No Change**, and moved forward (never back) to the date of a newer balance entry. Used by **Needs Attention** to flag accounts that haven't been updated within their category's limit |

#### balance_history

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, auto-increment |
| `account_id` | INTEGER | Foreign key to `accounts.id`. Deleting the account deletes its history |
| `balance` | TEXT | The amount, **encrypted** |
| `recorded_at` | TEXT | ISO 8601 date or date and time |

Each encrypted column holds `base64url(12-byte nonce || AES-256-GCM ciphertext || 16-byte tag)`.

#### app_config

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT | Primary key |
| `value` | TEXT | |

Stored keys:

| Key | Default | Notes |
|---|---|---|
| `master_hash` | none | Argon2id hash of the master password |
| `salt` | none | Hex-encoded 256-bit salt used to derive the key |
| `kdf_params` | none | The Argon2id settings used with `salt` to derive the key. Saved so they can be raised later without breaking existing data |
| `vault_format` | none | How fields are encrypted. `1` ties each field to its column. Missing means an older vault, which is upgraded at the next login |
| `key_check` | none | A test value encrypted with the vault key and decrypted at unlock to confirm the key is right |
| `lockout_started` | none | When the current login lockout started, as a POSIX timestamp |
| `lockout_duration` | none | How long that lockout lasts, in seconds. Stored with the start time instead of as an end time, so the wait can be capped and changing the clock can't extend it |
| `failed_login_attempts` | 0 | Failed attempts in a row. Saved so restarting the app doesn't hand back the attempts left before a lockout |
| `lock_timeout_minutes` | 15 | Minutes of inactivity before the vault locks |
| `clipboard_clear_seconds` | 30 | Seconds before a copied secret is cleared |
| `shutdown_after_lock_minutes` | 60 | Minutes after locking before the app closes, 0 for never |
| `theme` | dark | `dark` or `light` |
| `font_size` | default | `small`, `default`, `large`, or `xl` |
| `list_density` | compact | `compact` or `comfortable` |
| `auto_backup_enabled` | 0 | Whether automatic backups are on |
| `backup_frequency` | weekly | `daily`, `weekly`, or `monthly` |
| `backup_retention_months` | 12 | Months to keep automatic backups, 0 to keep them forever |
| `backup_location` | default | Backup folder. `default` means a `backups` folder next to the vault, wherever the vault is moved |
| `backup_confirmed_dir` | none | The folder you approved for backups, if you chose your own |
| `last_backup_date` | none | When the last automatic backup was made |
| `last_backup_outcome` | none | JSON: whether it worked, plus the file name or the error |
| `backup_files` | none | JSON list of the backups this app made. Old backups are only deleted if they're on this list |
| `backup_source` | none | The vault path the backup list belongs to. If it doesn't match, this vault is a copy, and the list it inherited is cleared |
| `backup_auth` | none | The seal: a value covering every backup row (`auto_backup_enabled` through `backup_source`), encrypted with the vault key |
| `warning_settings` | none | The **Manage > Warnings…** settings as JSON, **encrypted**. Missing means the defaults |
| `sort_key` | name | Account list sort order |
| `sort_descending` | depends on the sort | Whether the current sort is reversed |
| `window_geometry` | none | Hex-encoded result of `QMainWindow.saveGeometry()` |
| `sidebar_width` | none | Sidebar width in pixels, between 240 and 560 |

Everything after `key_check` is settings, window layout, and the backup record, not your data. It's stored unencrypted because the window has to be built and styled before you log in. There are two exceptions:

- `warning_settings` is encrypted because it contains the names of your custom categories. It's only needed while the vault is open.
- The backup rows stay readable, but they tell the app where to copy your vault and what it may delete. So `backup_auth` seals them, and rows that don't match the seal are reset instead of used.

The lockout rows have to be readable while the vault is locked. That also means anyone who can edit the database file can clear a lockout between guesses. The app can't prevent that. The real protection against guessing is how slow Argon2id makes each attempt; the lockout is there to slow down someone typing at the keyboard.

### Dependencies

| Package | Purpose |
|---|---|
| `PySide6` | Qt bindings for the desktop interface |
| `cryptography` | AES-256-GCM encryption |
| `argon2-cffi` | Argon2id key derivation |

SQLite comes with Python. `pytest` is the only development dependency. It's kept in `requirements-dev.txt`, so people who just want to use the app never have to install it.

Every version is pinned exactly, including the packages they depend on. `requirements.lock` goes further and pins the exact files, and `--require-hashes` checks them. That matters for an app that holds secrets, because a tampered package would run in the same process as the master password. The lock covers Windows and Linux x86-64. After changing a version in `requirements.txt`, run `python tools/genlock.py` and commit the result. CI installs from the lock, so a stale lock fails there instead of on someone's first install.

Pinning exact versions is only safe if something watches for new releases. `.github/dependabot.yml` opens a pull request when a pinned version falls behind or has a known security problem (a CVE), and `.github/workflows/tests.yml` runs the tests on it before it can be merged. The two crypto packages aren't grouped with anything else, so each update arrives on its own and gets reviewed by itself.
