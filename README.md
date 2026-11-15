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
- Each category has its own fields. Credit cards have a credit limit, minimum payment, due date, and interest rate. Loans add a lender, payoff date, and monthly payment. Investments have a contribution amount, how often you make it, and whose account it is. Property (a home, vehicle, or land) tracks an estimated value instead of a balance. Income tracks yearly pay.
- 401(k), 403(b), 457(b), IRA, SEP IRA, and SIMPLE IRA accounts have a **Tax Treatment**: Traditional, Roth, or Both.
  - For **Both**, enter the **Traditional Balance** and **Roth Balance** from your statement. The **Current Balance** is their total, and the form works it out for you. Contributions work the same way, with a **Traditional Contribution** and a **Roth Contribution**.
  - A Roth IRA, an HSA, and a brokerage account don't ask, because the type already says how it's taxed.
  - Changing an account to one of those types clears its tax treatment and the two amounts when you save. So does switching from Both to Traditional or Roth. The balance stays as it was.
- An account's details are grouped under **Account**, **Money**, **Login**, and **Notes**. A group with nothing in it isn't shown.
- Sensitive fields stay hidden until you click **Show**. These are passwords, card numbers, account and routing numbers, security answers, and the contents of secure notes.
  - Card, account, and routing numbers still show their last 4 characters while hidden (`•••• 4821`), so you can tell two cards apart. A number shorter than 8 characters stays fully hidden.
  - Security questions aren't hidden, because the question isn't the secret. Search still skips them.
- Copy buttons sit next to usernames, passwords, websites, and card, account, and routing numbers. On a hidden field, the button copies the value without showing it on screen. Only values copied with these buttons or the copy shortcuts (see **Keyboard shortcuts**) are cleared from the clipboard automatically. Text you select and copy by hand isn't.
- Every password field has a **Generate** button. You choose the length and which kinds of characters to include.
- **Quick Update** changes the balance and the due date without opening the full form. Use its **As Of** date to file a figure under the day your statement covers. An older figure only becomes the current balance if nothing newer is on file. Every Quick Update saves the balance to the history, even when it hasn't changed, unless that day already ends on the same figure. On a Both account, it asks for the Traditional and Roth amounts, and the new balance is their total. Press Enter in the first amount to move to the second.
- **Edit** opens the full form. Double-clicking the account's pane does the same, anywhere except on a value, where it selects a word to copy.
- **Mark Checked** marks an account as checked. It sets the Last Updated date to today and saves the current balance to the history, unless today already ends on the same figure.
- The arrows next to an account's name step to the previous or next account in the list.
- Click the star next to an account's name to pin it. A **Favorites** tab shows up while anything is pinned, and pinned accounts show a star in every list. Closed accounts keep their pin.
- You can mark an account as closed. Closed accounts move to the **Closed** tab and aren't counted in the dashboard totals.
- Two checkboxes leave an open account out of part of the app. Each works on its own, and neither hides the account anywhere else.
  - **Exclude from Dashboard** takes it out of every dashboard figure: balances, net worth, trends, and the investing rate. It's on banking, credit card, loan, investment, property, and income accounts. Use it for money that isn't yours, like an account you look after for a parent.
  - **Exclude from Retirement** takes it out of the **Retirement** tab only. It's on banking and investment accounts. Use it for money that isn't for your retirement, like a child's 529 or savings account.
- Only closed accounts can be deleted. An open account shows **Close Account**, which moves it to the **Closed** tab. There, the account shows **Reopen** and **Delete**. After you delete one, you have 10 seconds to undo it from the message that appears. After that, only a backup can bring it back.
- Shift-click or Ctrl-click to select several accounts, then close them, reopen them, or move them to another category all at once.

### Due dates

- A due date can repeat weekly, monthly, quarterly, or annually, and the app figures out the next one for you. New accounts in categories that have a due date start out monthly. Older accounts stay **One-off** (a one-time date) until you change them.
- Quick Update's **+1** button moves the date by the account's own cycle. A quarterly bill moves 3 months in one click, and an annual one moves a year.
- Each due date is worked out from the original date, so a bill due on the 31st shows as the 28th in February and goes back to the 31st in March.

### Finding things

- The tabs come in four groups, with a line between each: the lists drawn from every category (**All**, **Needs Attention**, and **Favorites**), then **Dashboard** and **Retirement**, then one tab per category, then **Closed** at the end.
- **Settings > Open On** picks the tab shown after you log in. Needs Attention and Favorites fall back to All while they're empty.
- Each row in the list shows when the account was last updated, like "Updated 3 days ago". Point at it to see the date. It turns orange or red when the account is due for an update.
- Dates are written with the month's name, like "Oct 24, 2026", so they can't be read two ways. Chart labels use the same style, like "May '26".
- Search looks across every category at once and shows which field matched, including category names. It doesn't search hidden fields, security questions, or notes, so nobody can use it to check a guess about a password or PIN.
- Sort by name, due date, or last updated, in either direction. The app remembers your choice.
- A **Needs Attention** tab appears when something is due within 14 days, is past due, or hasn't been updated in a while. It's hidden when there's nothing in it.
- After a Quick Update or **Mark Checked**, the account stays in Needs Attention so you aren't moved off it. Click the refresh button next to the list title to remove the accounts you've dealt with. The account you're viewing stays open if it still needs attention.
- How long counts as "a while" depends on the category: 3 weeks for credit cards, bank accounts, and utilities, 2 months for loans and investments, and 6 months for property and income. Websites, secure notes, and custom categories are never flagged unless you turn it on for them.
- A one-time due date that's more than 60 days past is shown as "lapsed" instead of in red. It's probably just out of date, not a missed bill.
- You can change all of these limits under **Manage > Warnings…**. You can set when due dates turn orange and red, when a one-time date counts as lapsed, and, for each category, whether due dates and updates are checked and after how many days. These settings are encrypted, because they include the names of your custom categories.

### Money tracking

- Every time an account's balance changes, a dated entry goes into its balance history. You can also add, edit, and delete entries yourself.
- The newest entry is the account's current balance. An entry dated on or after all the others updates the balance and moves the Last Updated date forward. An older entry just fills in the past. If you delete a wrong newest entry, the balance goes back to the one before it.
- On a Both account, changing the balance in its history doesn't change the Traditional and Roth amounts. The edit form then says they no longer add up to the balance. Enter both again from your latest statement to update them.
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
  - Beside the tables, your net worth is shown large, with how much it changed over the past year. Under it, a bar for each side shows what it's made of, in the categories' colors. Point at a part to see its category and amount. The last line says how much you owe as a share of what you own.
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

### Retirement planner

- The **Retirement** tab, next to **Dashboard**, shows the chance your savings last through retirement, worked out across 1,000 simulated markets.
- **Plan** shows that chance as a ring: **On track** at 85% or more, **Needs a tweak** from 65%, and **At risk** below that. Beside it are what you have invested today, your yearly spending (with any extra spending's total over the plan under it), the years until you retire, and the typical portfolio at retirement. Under what you have invested, a line says how much bank cash it includes and how much it leaves out, since it won't match the dashboard's Investments figure. A chart shows the typical portfolio by age and the range the middle 80% of markets fall in.
- **Next best moves** on **Plan** suggests changes that would help, such as working longer, spending less, delaying Social Security, or Roth conversions. They're worked out in the background after the plan appears. **Try it** opens **What-if** with the change already made.
- **What-if** has a slider for each of: retirement age (yours, and your partner's in a plan for two), yearly spending, average return, the age you claim Social Security, part-time income, and how many years of it. Each row also has a box to type an exact value, and shows your plan's own value beside it. It compares the result with your plan.
  - Each extra spending item gets its own slider under **Extra spending**, for its yearly amount. It runs from $0 to twice the item's amount (at least $20,000). When it starts and how long it lasts stay as entered in **Your details**.
  - The spending slider runs from half your plan's spending (or $30,000, if that's lower) to half again more (or $250,000, if that's higher), so there's room to try spending less. Nothing changes until you click **Apply to my plan**, which saves only the settings you moved.
- **Save scenario** keeps a what-if to compare later. A scenario keeps only the settings you changed, so it follows the rest of your plan as your balances and details change. There's room for 4. Delete one to save another.
- **Scenarios** shows your plan beside each saved scenario: the chance the money lasts, the typical outcome on one chart, and the figures side by side. **Make this my plan** applies a scenario's settings, and **Delete** removes it.
- **Projection** shows the spread by age in more detail: the typical portfolio, the middle 80% of markets, and poor markets (the 10th percentile), with markers where you retire, claim Social Security, and start a pension. Below it are where your money sits today, the assumptions behind the plan, and a table of the range at every fifth age.
- **Withdrawals & taxes** follows the plan with the average return every year. It shows which accounts pay for each retired year, what that costs in federal tax, and what Roth conversions would change.
  - Turn on **Convert pre-tax money to Roth in low-income years** here, and choose the bracket to fill: 12%, 22%, or 24%. Conversions are off until you turn them on.
  - If the money runs out, the chart and table stop at that year, and the page says when it runs out with and without conversions.
  - Each bar is the year's spending (extra spending included), health costs, and federal tax. From 75, a required distribution can take out more than the year needs. That makes the bar taller, and the extra goes back into the taxable account. The page says so under the chart's title.
  - If the lifetime federal tax comes to $0, the page says why: every year's taxable income stays within the standard deduction and the 0% rate on gains.
- **Your details** holds your birth month and year (and your partner's), when each of you will retire, how long the money should last, yearly spending, health premiums before 65, part-time income, Social Security, pensions, and market assumptions. Each change is saved as soon as you press Enter or leave the field.
- **Two of you** don't have to retire together:
  - **Partner retires at** is in your partner's own age. Leave it blank for the same year as you.
  - The plan starts drawing on your savings the first year either of you is retired. The one still working is paid **Your pay per year** or **Partner's pay per year**, and their contributions keep going out of that pay until they retire.
  - Contributions belong to whoever the account's **Whose** field says (blank is yours), so each person's stop at their own retirement.
  - Each person's health costs start at their own retirement.
  - If your partner retires first and your pay is blank, the years you keep working add nothing, and **Plan** says so.
  - **What-if** has a slider for each person's retirement age, and **Next best moves** suggests working longer or retiring earlier for each of you separately.
- **Extra spending in retirement** on **Your details** is for spending on top of your yearly spending, like travel, a new car, or a wedding.
  - Each item has a name, an amount per year in today's dollars, when it starts (years after you retire, where 0 is the year you retire), and how many years it lasts. A one-time purchase is 1 year.
  - Items count from your retirement age, so they move with it when you try a different age in **What-if**.
  - Years past the end of the plan aren't counted. There's room for 8 items.
  - **Scenarios** adds an **Extra spending** row with each one's total over the plan.
- **Pensions** on **Your details** take a monthly amount and the age it starts, for you and your partner. A pension pays for life and is taxed as ordinary income.
  - Tick **Rises with inflation** if the payment goes up each year. Leave it unticked if the payment stays the same. The planner then counts it as buying a little less each year, starting from today.
  - Income counts only once you retire, as with Social Security. A pension that starts sooner is counted from the year you retire.
  - **Withdrawals & taxes** adds a **Pension** column when you have one.
- Savings and yearly contributions come from your open accounts each time the tab opens:
  - 401(k), 403(b), 457(b), IRA, SEP IRA, and SIMPLE IRA accounts go by their **Tax Treatment**. One without a tax treatment counts as Traditional and is marked as assumed. On a Both account whose two amounts no longer add up to its balance, the amounts are scaled to fit and marked as estimated.
  - Roth IRAs count as Roth, HSAs as HSA, and brokerage accounts as taxable. Checking, savings, and money market accounts count as taxable cash.
  - Accounts of type Other or with no type aren't counted, and neither are overdrawn bank accounts or ones ticked **Exclude from Retirement** (see **Accounts**). Closed accounts are left out. **Your details** lists every account it used and every one it left out. Click one to open it.
  - A contribution amount with no period isn't counted, as on the dashboard.
  - An Investments account's **Whose** field (you or your partner) decides whose contributions it holds. **Your details** marks a partner's account in its list.
  - Type a figure into any savings or contribution field to use it instead of your accounts' figure. Clear the field to go back.
- Until your birth month and year and your yearly spending are entered, **Plan** shows a sample household and says what's missing. **View sample household** on **Your details** shows it again without changing your plan.
- The plan is encrypted in your vault, so a backup carries it and another vault never sees it.
- The planner gives educational estimates, not financial, tax, or legal advice. It models federal income tax only, using approximate 2026 brackets held steady in today's dollars, and simplifies Social Security, pensions, required distributions, and market returns. State taxes, Medicare surcharges, and ACA subsidies aren't included.

### Files, backups, and export

- You choose where your vault is saved. You can create a new one anywhere, open another, and switch between them. The app remembers the ones you've opened.
- A backup is a copy of the vault file, so it opens with the same master password. **Manage > Save a Copy…** makes one whenever you want. See **Backups and restoring** for automatic backups.
- **Manage > Export CSV…** saves all your accounts as a spreadsheet. It isn't encrypted, and the menu says so.
- **Manage > Import Accounts…** reads accounts from a CSV file. That can be this app's own export, another password manager's export, or your own spreadsheet. The first row must have column names. Before anything is saved, the app shows which field it matched to each column and which columns it skipped. Accounts already in the vault are skipped, so importing the same file twice doesn't add anything. Either the whole file is imported or none of it is.
- Each account's **Export** button offers 3 choices: everything, the account details only (name, username, password, website, and the category's own fields), or the balance history only. The two choices that include the password show a red warning and an **Export With Password** button. The history-only choice contains nothing that identifies the account, and it's the default. Categories without a balance only offer the details.

### Locking and appearance

- The vault locks after 5, 10, 15, 30, or 60 minutes of inactivity. Time asleep counts, and setting the computer's clock back doesn't extend it.
- On Windows it also locks right away when you lock your computer (Win+L, a screen saver that locks, or switching users) or the computer goes to sleep. On Linux it does the same when the desktop locks the screen or the computer goes to sleep, on desktops that announce it (KDE, GNOME, and most others). On macOS only the inactivity timer applies.
- After locking, the app can close itself after 30 minutes or 1, 2, or 4 hours, or stay open.
- Copied passwords are cleared from the clipboard after 15, 30, 60, or 120 seconds.
- On Windows and macOS, the app's windows are left out of screenshots, screen recordings, screen sharing, and Windows Recall. A capture shows a blank window instead. To take a screenshot or share your screen, turn on **Allow screenshots and screen sharing** in **Settings**. Linux has no way to do this.
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

### What the vault file shows without your password

Everything you type is encrypted, but someone with a copy of the vault file can still see its shape:

- How many accounts it holds, and which ones are closed.
- When each account was added and last updated.
- How many balance entries each account has, and their dates. The amounts themselves are encrypted.
- The size of long values, roughly. Every value is padded to a multiple of 64 bytes, so passwords, usernames, and balances all look the same size. A long note still shows its size, to the nearest 64 bytes.
- Your settings, such as theme and lock timeout. These have to be readable before you log in.

These are kept readable because sorting, **Needs Attention**, and the login screen depend on them.

## Requirements

- Python 3.11 or later. The verified install described in **Installation** supports 3.11 through 3.14.
- pip
- A computer running Windows, macOS, or Linux with a desktop (X11 or Wayland).

## Installation

**Windows:** run `AccountVault_Windows.bat`. The first time, it offers to install the packages it needs into a `.venv` folder inside the app's folder, without touching your system Python, and checks each one against a known fingerprint. If Python itself isn't installed, it tells you where to download it and gives you a command that installs it.

The app always runs from that `.venv`, even if your system Python already has the packages, because only the `.venv` was installed with the fingerprint check. When a newer version of the app changes the package list, the script offers to update the `.venv` before starting.

**Linux and macOS:**

```bash
git clone https://github.com/dgibbonsri/AccountVault2-PySide.git
cd AccountVault2-PySide
./AccountVault_Mac_Linux.sh
```

The script works the same way as the Windows one: it always runs the app from a `.venv` folder inside the app's folder, offers to set that up the first time, and offers to update it when the package list changes. Run it from a terminal the first time, because it needs to ask before installing. Started without a terminal, it runs whatever is already installed.

The script checks every downloaded file against a known fingerprint (`--require-hashes`). Those fingerprints cover Windows and Linux on x86-64 and macOS on Apple silicon, with Python 3.11 through 3.14. On an Intel Mac (`cryptography` publishes no wheel for it), ARM Linux, or a newer Python, the script says so and asks before installing the same pinned versions without the check.

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

A demo vault is full of made-up accounts for a fictional household. It has 31 months of balance history, every kind of repeating due date, out-of-date and lapsed accounts, pinned and closed accounts, a custom category, every Tax Treatment, both Exclude checkboxes, and a saved retirement plan, so every screen has something on it.

To build one, use either of these. For safety, neither will overwrite a file that already exists, so to replace an old demo, delete it yourself first.

- Click **Create Demo Vault…** on the vault chooser. Fill in the household (see "Demo household" below) and pick where the vault goes. The app shows you the password, then asks for it to unlock the new vault.
- Run `python tools/make_demo_vault.py [path]`. It uses the default household. With no path, it writes `springfield.vault` in the current folder. It prints the password when it's done. Open the vault with **Manage > Open Vault…** or `python main.py path/to/springfield.vault`.

The balance history and due dates count back from the day it's built, so a fresh one looks current.

A demo vault's password is published with the app, so the login screen and the top bar both say **Demo vault** while one is open. Don't keep real accounts in it.

#### Demo household

Before building, **Create Demo Vault…** asks about the household:

- **Household:** single income (Homer works, Marge doesn't) or two incomes (both work)
- **Homer's age** and **Marge's age**
- **Yearly income:** both jobs together
- **Total invested:** every Investments account together
- **Retirement saving:** every contribution together, as a % of income

What these change:

- **Income:** one job or two. Earlier side incomes are there as closed accounts, so they don't count.
- **Investments:** balances and contributions are spread across a 401(k), a 403(b) with two incomes, IRAs, a Roth IRA, an HSA, a brokerage account and a few others. Contributions stop at the yearly limits, and anything over goes to the brokerage account.
- **Banking:** the household's bank balances grow with income. The children's accounts stay the same.
- **Retirement plan:** birth dates from the ages, spending (take-home pay minus saving), and a rough Social Security estimate. Marge gets half of Homer's if she doesn't work.

The totals come out close to your figures but not exactly, as real accounts do. The same answers always build the same vault.

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
| Key derivation | Argon2id with 128 MiB of memory, 4 iterations, and 2 lanes. These settings are saved in the vault, so they can be raised later. The vault re-encrypts itself with the new settings at the next login. Settings read from a vault, including those inside the password hash, must fall within fixed limits, so a crafted vault can't freeze the computer that opens it |
| Field encryption | AES-256-GCM, with a new random 96-bit nonce every time a field is encrypted |
| Field binding | Each field is tied to its table, column, and row, so encrypted data moved anywhere else won't decrypt. This stops someone who can edit the database file from making a note show up as a password, swapping two accounts' passwords or routing numbers, or moving a balance entry to a different account |
| Padding | Each value is padded to a multiple of 64 bytes before it's encrypted, so the file doesn't show how long a password is or how many digits a balance has |
| Values written outside the app | Plaintext found in an encrypted column of a current vault shows as unreadable and is never encrypted, so someone who edits the file can't plant a website or account number that looks real. The app warns after unlocking if it finds any |
| Database code | SQLite can store triggers inside the file that run on every save. The app creates none, so a vault holding one (or a view, or a table or index the app didn't make) is refused, checked on every connection. Every connection also turns on SQLite's `trusted_schema` off and defensive mode |
| Key storage | In memory only and never written to disk. Released when the vault locks, but not erased from memory (see **What locking doesn't do**) |
| Salt | A 256-bit random value stored in the database |
| Key check | At every unlock, the app decrypts a known test value to confirm the key is right. Without it, a password and salt that don't belong together (from a half-restored backup, for example) would log in, show every field as unreadable, and the first save would write that over the real data |
| Auto-lock | After a set time with no activity, measured by whichever of the wall clock and the monotonic clock says longer, so neither setting the clock back nor sleeping stretches it. Right away when the session locks or the computer is about to sleep: Windows messages on Windows, D-Bus signals on Linux (`org.freedesktop.ScreenSaver`, `org.gnome.ScreenSaver`, and logind's `PrepareForSleep`). Not on macOS. Locking releases the key and drops the decrypted data |
| Network | None. The app opens no sockets and makes no connections |
| Attack surface | One process. Decrypted data never passes to another process |
| Login throttling | 5 failed attempts cause a lockout that starts at 30 seconds and doubles up to 5 minutes. The lockout and the failure count are both saved in the database, so restarting the app resets neither. The lockout is stored as a start time and a length, and the remaining wait is capped, so changing the system clock can't make it longer |
| Clipboard | Copied secrets are cleared automatically, but only if the clipboard still holds what the app copied. They're also cleared when the app quits or the session ends, since Windows keeps clipboard data after the program that put it there exits. They're marked to stay out of Windows clipboard history and cloud sync, and out of KDE's clipboard history |
| Screen capture | Every window, dialogs and menus included, is excluded from capture as it first appears: `SetWindowDisplayAffinity` on Windows (blacked out instead on Windows before 10 version 2004), `NSWindow.sharingType` on macOS. On by default, and can be turned off per vault in **Settings**. Linux has no equivalent |
| Database file | Only the owner's account can access it: mode 0600 on Linux and macOS, and an access control list with inherited permissions removed on Windows. It's kept apart from the app's code (see **Where the vault is stored**). SQLite's journal file beside it (`name.vault-journal`) is kept and restricted the same way, so it never takes the folder's permissions. It's empty between saves and safe to leave out of a copy |
| Vault folder | Locking down the file doesn't help if others can change its folder, since they could replace the file. The app checks the folder each time a vault opens and warns if other accounts can create or delete files there |
| Deleted data | SQLite's `secure_delete` overwrites data as it's deleted, so a deleted account doesn't linger in the file. The one-time format upgrade also compacts the file, which clears anything deleted before `secure_delete` was turned on |
| Sealed settings | The backup settings control where copies of the whole vault go and which files can be deleted. Four more decide how exposed an open vault is: the lock timeout, clipboard clearing, auto-shutdown after a lock, and screen capture. Anyone who can edit the vault file could change any of them without the password, so the app protects them with a seal made with the vault key, checked as soon as the vault is unlocked. If the seal doesn't match, they're reset to the defaults, automatic backups are turned off, and the app tells you. Until the check, screen capture stays blocked and auto-shutdown is never longer than the default. A backup folder you choose yourself is confirmed once before the first copy goes there |
| Undo after delete | During the 10 seconds you can undo a delete, the deleted data is held still encrypted, so it's no more exposed than the vault file itself. It's discarded when the vault locks or you switch vaults, because data encrypted with one vault's key must never be restored into another vault |
| Dependencies | Installed with `--require-hashes` against `requirements.lock`, which checks the exact files and not just the version numbers. A tampered `cryptography` or `argon2-cffi` package would run in the same process as the master password |
| Master password | At least 15 characters, with no space at the start or end. It's the only password: a backup is a copy of the vault and opens with the same one |
| CSV export | Values a spreadsheet would run as a formula are made safe before they're written, including lookalikes such as a full-width `＝` or a space before `=`. A per-account export says what it includes before saving and warns when the file will contain the password. The history-only export holds just dates and amounts |
| CSV import | Every column is listed with the field it will be read as before anything is saved, so a wrongly matched column is easy to spot. Columns the app can't match are skipped. Nothing is decrypted or saved until you click **Import**, and the dialog reminds you that the source file is still unencrypted |
| Damaged data | A field that won't decrypt shows a placeholder instead of the raw encrypted text. The placeholder is never saved as a value, whether by a save or by importing a CSV exported from a damaged vault |
| Displayed values | Stored values are always shown as plain text. A value that looks like HTML (`xK9<b>mq2`) is shown exactly as stored |
| Value lengths | A value that's too long for its field is rejected with a message naming the field. It's never cut short without telling you |
| Error log | Unexpected errors are shown on screen and written to `errors.log` next to `vaults.json`, readable only by you. The log records the error type and where it happened, never the error message or any values, because a message can include field contents. Errors printed to the terminal are redacted the same way. For development, set `ACCOUNTVAULT_DEBUG=1` to print full tracebacks there; the log stays redacted |

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

Most tests cover `services/`, `core/auth.py`, `core/encryption.py`, and `core/preferences.py`. Those are where a bug could destroy data that can't be recovered: balance history, vault copies and how long they're kept, the backup seal, the decrypted account cache, password changes and format upgrades, login throttling, plaintext written outside the app, and fields that won't decrypt.

`core/retirement.py`, the math behind the retirement planner, is tested against tax figures worked out by hand from the 2026 tables.

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
│   ├── retirement.py        # Retirement planner math: tax, withdrawals, markets
│   ├── vaultfile.py         # Which vault to open, and the recent list
│   ├── urls.py              # Blocks unsafe link types, strips mailto: options
│   └── version.py           # The application version
├── services/
│   ├── accounts.py          # Account CRUD, balance history, decrypted cache
│   ├── dashboard.py         # Balance sheet, monthly trends, investing vs. income
│   ├── retirement.py        # The stored retirement plan, savings from the accounts
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
│   ├── retirement_view.py   # The Retirement tab: view buttons, sample banner
│   ├── retirement_pages.py  # Retirement Plan, Projection, Withdrawals & taxes
│   ├── retirement_whatif.py # Retirement What-if and Scenarios
│   ├── retirement_details.py # Retirement Your details: the plan's inputs, saved as you go
│   ├── retirement_charts.py # The planner's charts: portfolio band, stacked years, odds ring
│   ├── ranges.py            # Range buttons and change lines, shared by both views
│   ├── dialogs.py           # Settings, password, imports, document viewer, about
│   ├── widgets.py           # Charts, toasts, password generator, tabs
│   ├── screen_capture.py    # Keeps the windows out of screenshots and screen sharing
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
| `category` | TEXT | The category name, for example "Credit Cards", "Banking", or a custom one, **encrypted**. Each row is encrypted separately, so the file doesn't show which accounts share a category |
| `name` | TEXT | Account name, **encrypted** |
| `username` | TEXT | **Encrypted** |
| `password` | TEXT | **Encrypted** |
| `website` | TEXT | **Encrypted** |
| `notes` | TEXT | **Encrypted** |
| `extra_data` | TEXT | The category's own fields (balances, due dates and how they repeat, rates) plus the Favorites pin, stored as JSON and **encrypted** as one value. Keeping the pin here means the file doesn't reveal which accounts you use most |
| `closed` | INTEGER | 0 for open, 1 for closed |
| `date_added` | TEXT | YYYY-MM-DD, set when the account is created. Not shown anywhere in the app |
| `last_updated` | TEXT | YYYY-MM-DD. Set to today by an edit or **Mark Checked**, and moved forward (never back) to the date of a newer balance entry. Used by **Needs Attention** to flag accounts that haven't been updated within their category's limit |

#### balance_history

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, auto-increment |
| `account_id` | INTEGER | Foreign key to `accounts.id`. Deleting the account deletes its history |
| `balance` | TEXT | The amount, **encrypted** |
| `recorded_at` | TEXT | ISO 8601 date or date and time |

Each encrypted column holds `base64url(12-byte nonce || AES-256-GCM ciphertext || 16-byte tag)`. The plaintext is a 4-byte length, the value, and zeros up to a multiple of 64 bytes. The associated data names the table, column, and row: `accounts.password:17`, or `balance_history.balance:17:203` for entry 203 of account 17.

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
| `vault_format` | none | How fields are encrypted. `1` ties each field to its column. `2` also encrypts the category. `3` also ties each field to its row and pads it. Missing or older means an older vault, which is upgraded at the next login |
| `key_check` | none | A test value encrypted with the vault key and decrypted at unlock to confirm the key is right |
| `lockout_started` | none | When the current login lockout started, as a POSIX timestamp |
| `lockout_duration` | none | How long that lockout lasts, in seconds. Stored with the start time instead of as an end time, so the wait can be capped and changing the clock can't extend it |
| `failed_login_attempts` | 0 | Failed attempts in a row. Saved so restarting the app doesn't hand back the attempts left before a lockout |
| `lock_timeout_minutes` | 15 | Minutes of inactivity before the vault locks |
| `clipboard_clear_seconds` | 30 | Seconds before a copied secret is cleared |
| `shutdown_after_lock_minutes` | 60 | Minutes after locking before the app closes, 0 for never |
| `allow_screen_capture` | 0 | 1 lets screenshots and screen sharing capture the app's windows |
| `theme` | dark | `dark` or `light` |
| `font_size` | default | `small`, `default`, `large`, or `xl` |
| `list_density` | compact | `compact` or `comfortable` |
| `start_tab` | all | The tab shown after login: `all`, `attention`, `favorites`, `dashboard`, or `retirement` |
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
| `retirement_plan` | none | The retirement plan's details (extra spending items included), any savings figures you typed in, and saved scenarios, as JSON, **encrypted**. Missing means no plan yet |
| `sort_key` | name | Account list sort order |
| `sort_descending` | depends on the sort | Whether the current sort is reversed |
| `window_geometry` | none | Hex-encoded result of `QMainWindow.saveGeometry()` |
| `sidebar_width` | none | Sidebar width in pixels, between 240 and 560 |

Everything after `key_check` is settings, window layout, and the backup record, not your data. It's stored unencrypted because the window has to be built and styled before you log in. There are three exceptions:

- `warning_settings` is encrypted because it contains the names of your custom categories. It's only needed while the vault is open.
- `retirement_plan` is encrypted because it holds your birth dates, spending, Social Security, and pensions. It's only needed while the vault is open.
- The backup rows stay readable, but they tell the app where to copy your vault and what it may delete. So `backup_auth` seals them, and rows that don't match the seal are reset instead of used.

The lockout rows have to be readable while the vault is locked. That also means anyone who can edit the database file can clear a lockout between guesses. The app can't prevent that. The real protection against guessing is how slow Argon2id makes each attempt; the lockout is there to slow down someone typing at the keyboard.

### Dependencies

| Package | Purpose |
|---|---|
| `PySide6_Essentials` | Qt bindings for the desktop interface. The core modules only: the full `PySide6` package also installs Qt WebEngine (a whole Chromium) and other add-ons the app never uses |
| `cryptography` | AES-256-GCM encryption |
| `argon2-cffi` | Argon2id key derivation |

SQLite comes with Python. `pytest` is the only development dependency. It's kept in `requirements-dev.txt`, so people who just want to use the app never have to install it.

Every version is pinned exactly, including the packages they depend on. `requirements.lock` goes further and pins the exact files, and `--require-hashes` checks them. That matters for an app that holds secrets, because a tampered package would run in the same process as the master password. The lock covers Windows and Linux on x86-64 and macOS on Apple silicon. If a platform drops out (a package stops publishing a wheel for it), `tools/genlock.py` prints a warning. After changing a version in `requirements.txt`, run `python tools/genlock.py` and commit the result. CI installs from the lock, so a stale lock fails there instead of on someone's first install.

Pinning exact versions is only safe if something watches for new releases. `.github/dependabot.yml` opens a pull request when a pinned version falls behind or has a known security problem (a CVE), and `.github/workflows/tests.yml` runs the tests on it before it can be merged. The two crypto packages aren't grouped with anything else, so each update arrives on its own and gets reviewed by itself.
