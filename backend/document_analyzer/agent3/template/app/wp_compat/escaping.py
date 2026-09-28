"""Escaping helpers — design_agent3.md §8. Stateless, so both wp_compat backends
share this implementation directly (no `WPCompatBackend` method for these).
"""

import html
from urllib.parse import quote


def esc_html(value) -> str:
    return html.escape(str(value), quote=False)


def esc_attr(value) -> str:
    return html.escape(str(value), quote=True)


def esc_url(value) -> str:
    return quote(str(value), safe=":/?&=#%")


def sanitize_text_field(value) -> str:
    return " ".join(str(value).split())
