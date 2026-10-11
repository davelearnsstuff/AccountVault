# -----------------------------------------------------------------------------
# core/urls.py
# Deciding whether a stored website value is safe to hand to the operating
# system, and what to hand it. The check standing between a value in the vault
# and QDesktopServices.openUrl.
# -----------------------------------------------------------------------------

import re
from urllib.parse import unquote


# Schemes that execute rather than navigate. Matched against a normalized copy
# of the scheme: browsers and Qt ignore whitespace and control characters
# inside a scheme and decode percent-escapes, so 'java\tscript:' and
# '%6Aavascript:' launch the same handler 'javascript:' does.
_BLOCKED_SCHEMES = frozenset({'javascript', 'vbscript', 'data', 'blob', 'file', 'about'})
_SCHEME = re.compile(r'^([^:/?#]+):')
_SCHEME_NOISE = re.compile(r'[\s\x00-\x1f\x7f]')


def _scheme_of(text: str) -> str | None:
    """The scheme a browser would read off this text, lowercased, or None."""
    match = _SCHEME.match(text)
    if not match:
        return None
    raw = match.group(1)
    try:
        raw = unquote(raw)
    except Exception:
        pass
    return _SCHEME_NOISE.sub('', raw).lower()


def normalize_url(value) -> str:
    """Prepends https:// when no safe scheme is present. Blocks script-ish schemes.

    Returns '' for a blocked scheme, which the detail view renders as plain,
    unlinked text."""
    if not value:
        return ''
    text = str(value).strip()
    if not text:
        return ''

    if _scheme_of(text) in _BLOCKED_SCHEMES:
        return ''
    if re.match(r'^(https?://|mailto:)', text, re.I):
        return text
    # Everything else is treated as a hostname and prefixed. Unknown schemes are
    # not refused: 'example.com:8080' is a host and a port, and is
    # indistinguishable from a scheme.
    return f"https://{text}"
