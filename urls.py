# -----------------------------------------------------------------------------
# urls.py
# Deciding whether a stored website value is safe to hand to the operating
# system, and what to hand it.
#
# Its own module rather than a corner of categories.py, where it used to live.
# categories.py is field schemas, attention rules and value formatting — the
# file a contributor edits to add a category — and this is none of those: it is
# the check standing between a value in the vault and QDesktopServices.openUrl.
# A security control is worth being findable as one, beside encryption.py and
# auth.py rather than under a heading about display.
# -----------------------------------------------------------------------------

import re
from urllib.parse import unquote


# Schemes that execute rather than navigate. Matched against a normalized copy
# of the scheme rather than the raw text: a browser and Qt both ignore ASCII
# whitespace and control characters inside a scheme, so 'java\tscript:' and
# 'java\nscript:' launch the same handler 'javascript:' does, and percent-escapes
# are decoded before the scheme is read. Comparing the raw prefix let all three
# through the check and then straight into QDesktopServices.openUrl.
_BLOCKED_SCHEMES = frozenset({'javascript', 'vbscript', 'data', 'blob', 'file', 'about'})
_SCHEME = re.compile(r'^([^:/?#]+):')
_SCHEME_NOISE = re.compile(r'[\s\x00-\x1f\x7f]')


def _scheme_of(text: str) -> str | None:
    """The scheme a browser would read off this text, lowercased, or None.

    Percent-escapes are decoded first, and whitespace and control characters
    stripped, so the answer is what actually gets dispatched rather than what
    the string looks like."""
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

    Returns '' for a blocked scheme. The caller renders that as a plain
    unlinked value — see detail_view — so a refusal is visible as a link that
    isn't one rather than silently swallowing the field."""
    if not value:
        return ''
    text = str(value).strip()
    if not text:
        return ''

    if _scheme_of(text) in _BLOCKED_SCHEMES:
        return ''
    if re.match(r'^(https?://|mailto:)', text, re.I):
        return text
    # Everything else is treated as a hostname and prefixed, as before. Note
    # this deliberately does not refuse unknown schemes: 'example.com:8080' is
    # a host and a port, and is syntactically indistinguishable from a scheme.
    return f"https://{text}"
