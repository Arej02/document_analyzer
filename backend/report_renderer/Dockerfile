# Assessment report PDF renderer (design_report.md §8.3).
# Build: uv run document-analyzer-build-report-image
#   (or: docker build -t galvanise-report-renderer:0.1 backend/report_renderer)
# The base image is pinned and ships Chromium and its system libraries, but not the Python client; the
# `playwright` pin below must match the base image's tag (the browser build is tied to it). No USER / ENTRYPOINT here: report/pdf.py sets the user, the limits,
# the mounts and the command on `docker run`, and the container only ever runs /opt/render_pdf.py.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

RUN pip install --no-cache-dir --break-system-packages playwright==1.49.1

COPY render_pdf.py /opt/render_pdf.py
RUN chmod 0644 /opt/render_pdf.py
