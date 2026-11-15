# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AccountVault is a local-only password manager and money tracker: a PySide6 desktop app over a single SQLite `.vault` file in which every user-entered field is AES-256-GCM encrypted under an Argon2id-derived key. No network, no server, one process.

## Commands

Development happens on Windows; use the project's `.venv` (the launchers never use a system Python).

```bash
# Install (hash-checked, same as CI and the launchers)
python -m pip install --require-hashes -r requirements/requirements.lock -r requirements/requirements-dev.lock

# Run
python main.py                      # last-used vault
python main.py path/to/my.vault     # a specific vault (or set ACCOUNTVAULT_DB)
ACCOUNTVAULT_DEBUG=1 python main.py # full tracebacks to stderr; errors.log stays redacted

# Tests
python -m pytest tests/ -q
python -m pytest tests/test_backups.py -q
python -m pytest tests/test_backups.py::test_name -q

# Demo vault (refuses to overwrite; prints the password)
python tools/make_demo_vault.py [path]

# After changing any pin in requirements/*.txt, regenerate the locks and commit them
python tools/genlock.py
```

There is no linter or formatter configured. CI (`.github/workflows/tests.yml`) runs pytest on Windows with Python 3.11 and 3.14.

## Architecture

Layers, imports strictly downward: `main.py` → `ui/` → `services/` → `core/`.

- `core/` imports no Qt and nothing from `services/` or `ui/`. `tests/test_layers.py` enforces this by reading source, so a function-local import also fails. `tools/make_demo_vault.py` builds vaults headless and depends on this.
- `ui/` holds no SQL and no crypto. `services/` holds account CRUD, balance history, the decrypted account cache, backups, dashboard figures, the stored retirement plan, and CSV import/export.
- The main window is `ui/main_window.py` plus three mixins: `actions.py` (commands, menus, appearance), `vault_session.py` (which vault, lock state, timers), `account_list.py` (list, tabs, search, sort, bulk actions). Each file's header says which window state it owns.
- `core/retirement.py` is pure math ported from Runway (`docs/runway.html`), following it down to calculation order; a test pins Runway's sample household. Markets are seeded, so results are deterministic.

### Data model

- Three tables: `accounts`, `balance_history`, `app_config`. Everything category-specific lives in `accounts.extra_data`, a JSON blob encrypted as one value. Callers of `services/accounts.py` work with flat dicts; `split_flat` does the split.
- `core/categories.py` is the schema: per-category fields, hidden/copyable/link flags, staleness limits, `shown_when` / `hidden_when` conditional fields. Adding a category or field is an entry there, not a new column or table.
- Plaintext columns are deliberately limited to structure (`closed`, `date_added`, `last_updated`, `recorded_at`, and non-content settings). Don't promote content (due dates, favorites, category) to a plaintext column.
- Balance history drives the current balance: the newest entry is the balance, and `last_updated` only moves forward.

### Encryption invariants (easy to break silently)

- Every AES-GCM envelope is built in `encryption.encrypt_with`, the only place nonces are generated.
- Every field's associated data binds table, column, and row (`row_aad`, e.g. `accounts.password:17`, `balance_history.balance:17:203`), and plaintext is padded to 64 bytes. A field can't be encrypted before its row has an id, so new rows are inserted with empty fields and filled in the same transaction.
- Every `app_config` write goes through `database.write_config` (reads through `read_config`); they accept an open connection so re-keys are one transaction.
- Re-keying (password change, KDF upgrade) must re-encrypt the encrypted `app_config` rows too: `warning_settings`, `retirement_plan`, `backup_auth`, `key_check`. A new encrypted config row must be added to those paths.
- `vault_format` is currently 3. Older vaults are upgraded at login in `auth.migrate_vault_format`. Plaintext found in an encrypted column of a current vault is shown as unreadable and never encrypted.
- Never edit `LEGACY_KDF_PARAMS`; it reproduces keys for old vaults.
- Unreadable fields show a placeholder that must never be saved back; saves keep the stored ciphertext instead.
- The schema guard refuses any trigger, view, unknown table, or non-SQLite index on every connection. Creating new schema objects means updating that allowlist in `core/database.py`.
- Error logs and stderr record only the exception type and location, never the message (messages can contain field values).
- Say "release," never "wipe," for what locking does to the key.

### Other rules

- Select options (e.g. Account Type) may only be appended, never renamed or removed: the form blanks unknown stored values on save. A test checks every option ever offered is still offered.
- Exclude checkboxes and similar ticks are stored as the string `'Yes'`, blank when unticked.

## Tests

- `tests/conftest.py` sets `QT_QPA_PLATFORM=offscreen`, isolates the app data dir per test, and provides `cheap_argon2` to drop the KDF to its floor (real params cost about a second per derivation). Use it for any test that creates a vault; `test_rotation.py` checks the real parameters.
- Coverage is weighted to `services/` and `core/auth.py`, `encryption.py`, `preferences.py`, where bugs destroy unrecoverable data. Most of `ui/` is untested on purpose, except widgets that handle stored values or secrets directly (account form, Your details, clipboard), which are tested like services. See the header of `tests/test_ui.py`.

## Docs

- `README.md` (what/how, security table, schema) and `docs/AccountVault_Design_Document.md` (why) describe the app as built and are viewable in-app under Manage. Update them alongside behavior, schema, or `app_config` key changes.
- Writing style for both: plain English, short bullets over paragraphs, no em dashes, US spelling, and cross-references name the section in bold (e.g. see **Layers**).
