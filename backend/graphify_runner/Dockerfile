# Graphify extraction worker for Agent 1 (ported from Galvanise's infra/worker).
# Built by `uv run document-analyzer-build-graphify-image`, which sends a curated build
# context (this file, the hash-locked requirements, and the stdlib-only
# document_analyzer/graphify_worker package) instead of the repository, so credentials,
# job data and the rest of the backend can never enter the image.
#
# Base pinned by manifest-list digest; the build is native to the host architecture.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${PYTHON_IMAGE} AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
# Hash-checked wheels; optional parser grammars without a published wheel compile here.
RUN pip wheel --no-cache-dir --require-hashes -r /tmp/requirements.lock -w /wheels

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    GRAPHIFY_MAX_WORKERS=1
# Wheels were hash-verified (or compiled from hash-verified sdists) in the build stage;
# a locally compiled wheel cannot match its sdist hash, so install the exact set offline.
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --no-deps /wheels/*.whl \
 && pip check \
 && rm -rf /wheels \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin graphify
COPY graphify_worker /opt/graphify_worker/graphify_worker
ENV PYTHONPATH=/opt/graphify_worker
# Capability manifest generated at build time from the installed release; every advertised
# extension is smoke-parsed, and the build fails if one does not work.
RUN python -m graphify_worker capabilities > /opt/graphify_worker/capabilities.json \
 && python -m graphify_worker check-capabilities /opt/graphify_worker/capabilities.json
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "graphify_worker"]
CMD ["analyse"]
