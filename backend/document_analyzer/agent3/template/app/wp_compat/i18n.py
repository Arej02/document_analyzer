"""i18n no-ops — design_agent3.md §8. The generated app has no translation
catalog, so these return the original string unchanged. Stateless, so both
wp_compat backends share this implementation directly.
"""

from .escaping import esc_html


def __(text: str, domain: str | None = None) -> str:
    return text


def _e(text: str, domain: str | None = None) -> None:
    # WordPress's _e() echoes into the current template; there's no rendering
    # context to echo into here, so this is a no-op rather than printing into
    # captured test output.
    return None


def esc_html__(text: str, domain: str | None = None) -> str:
    return esc_html(__(text, domain))
