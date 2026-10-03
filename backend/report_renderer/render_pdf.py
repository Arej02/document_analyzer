"""Runs inside the report renderer container: /in/report.html -> /out/report.pdf (design_report.md §8.3).

The HTML is our own rendered report, but it contains text from the analysed repository, so
Chromium is treated as if it were rendering something hostile:
- JavaScript is disabled for the context.
- Every request except the one input file and `data:` URLs is aborted by Chromium's own request
  interception, and the container has no network besides.
- Downloads and service workers are off, and the process runs as an unprivileged user with a
  read-only root (`--no-sandbox` is needed because the container, not Chromium, is the sandbox:
  all capabilities are dropped, so Chromium cannot create its own).
Only two environment values come in (REPORT_ID, REPORT_DATE), for the running header; they are
HTML-escaped here. Nothing is written but the PDF.
"""

import html
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SOURCE = Path("/in/report.html")
TARGET = Path("/out/report.pdf")
SOURCE_URL = SOURCE.as_uri()


def header_html(report_id: str, report_date: str) -> str:
    style = "font-size:8px;font-family:Helvetica,Arial,sans-serif;color:#5d6573;width:100%;padding:0 14mm;box-sizing:border-box;"
    return (
        f'<div style="{style}display:flex;justify-content:space-between;">'
        f"<span>Report {html.escape(report_id)}</span><span>{html.escape(report_date)}</span></div>"
    )


def footer_html() -> str:
    style = "font-size:8px;font-family:Helvetica,Arial,sans-serif;color:#5d6573;width:100%;padding:0 14mm;box-sizing:border-box;text-align:right;"
    return f'<div style="{style}">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>'


def main() -> int:
    report_id = os.environ.get("REPORT_ID", "")[:64]
    report_date = os.environ.get("REPORT_DATE", "")[:32]
    blocked: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions", "--disable-background-networking"]
        )
        try:
            context = browser.new_context(java_script_enabled=False, accept_downloads=False, service_workers="block")

            def gate(route):
                url = route.request.url
                if url == SOURCE_URL or url.startswith("data:"):
                    route.continue_()
                else:
                    blocked.append(url[:200])
                    route.abort()

            context.route("**/*", gate)
            page = context.new_page()
            page.goto(SOURCE_URL, wait_until="load")
            page.emulate_media(media="print")
            page.pdf(
                path=str(TARGET),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template=header_html(report_id, report_date),
                footer_template=footer_html(),
                margin={"top": "20mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
            )
        finally:
            browser.close()

    for url in blocked:
        print(f"blocked request: {url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
