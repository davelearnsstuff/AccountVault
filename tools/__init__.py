# -----------------------------------------------------------------------------
# tools/__init__.py
# Makes tools/ a package, so the app can import make_demo_vault for the vault
# chooser's Create Demo Vault… button.
#
# A regular package rather than a namespace one: a namespace package loses to
# any installed package that happens to be called `tools`, and plenty are.
# -----------------------------------------------------------------------------
