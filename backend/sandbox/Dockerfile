# Build from the repository root:
#   podman build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
#
# Both bases are immutable multi-architecture OCI references. Keep them in sync
# with runtime-lock.json.
FROM docker.io/library/node@sha256:b04ce4ae4e95b522112c2e5c52f781471a5cbc3b594527bcddedee9bc48c03a0 AS node_runtime

FROM docker.io/library/python@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

LABEL org.opencontainers.image.title="CAD Agent unified MCAD runtime" \
      org.opencontainers.image.description="Pinned CadQuery, build123d, OCP, DXF, implicit-CAD and headless snapshot worker" \
      io.wordswave.mcad.runtime.schema="mcad-runtime-lock.v1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

# Node is copied from a versioned official image instead of installing a mutable
# distro package. The Python and Node stages share a Debian bookworm ABI.
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY backend/sandbox/runtime-requirements.txt /opt/cad-agent/runtime-requirements.txt

# OCP/OpenCascade runtime libraries plus the complete, exact Python dependency
# closure. The cache mount is not committed to the image; it only lets a
# transient download failure resume without weakening pip's index hash checks.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libgl1 libglib2.0-0 libxrender1 && \
    rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --only-binary=:all: \
      --requirement /opt/cad-agent/runtime-requirements.txt && \
    python -m pip install --only-binary=:all: --no-deps \
      build123d==0.11.1 cadquery==2.8.0

# Install only the headless browser used for deterministic PNG rendering.
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0 python -m playwright install \
      --with-deps chromium-headless-shell && \
    rm -rf /var/lib/apt/lists/* /root/.cache /tmp/*

COPY backend/sandbox/runtime-lock.json /opt/cad-agent/runtime-lock.json

# build123d imports its optional lib3mf adapter eagerly, but lib3mf has no Linux
# ARM64 distribution. Patch only that import boundary; Mesher remains present
# and fails explicitly while all verified STEP/STL modeling APIs remain real.
COPY backend/sandbox/patch_build123d.py /opt/cad-agent/patch_build123d.py
RUN python /opt/cad-agent/patch_build123d.py

# Bundle the real vendored CAD/DXF/implicit-CAD command implementations. Task
# dispatch is added at the ExecutionBackend boundary; no host checkout is needed
# inside a worker.
COPY third_party/cadskills/skills /opt/cadskills/skills

RUN python -m pip install --no-cache-dir --no-deps \
      /opt/cadskills/skills/cad/scripts/packages/cadpy && \
    cd /opt/cadskills/skills/implicit-cad/scripts/packages/implicitjs && \
      PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --omit=dev && \
    cd /opt/cadskills/skills/cad-viewer/scripts/viewer/packages/cadjs && \
      npm ci --omit=dev && \
    chmod -R a+rX /opt/cadskills /opt/cad-agent /ms-playwright

COPY backend/sandbox/executor_entry.py /opt/cad-agent/executor_entry.py
COPY backend/sandbox/capability_entry.py /opt/cad-agent/capability_entry.py
COPY backend/sandbox/runtime_probe.py /opt/cad-agent/runtime_probe.py

RUN groupadd --gid 1000 sandbox && \
    useradd --uid 1000 --gid 1000 --no-create-home \
      --home-dir /tmp --shell /usr/sbin/nologin sandbox && \
    chown 1000:1000 /opt/cad-agent/executor_entry.py \
      /opt/cad-agent/capability_entry.py /opt/cad-agent/runtime_probe.py

USER 1000:1000

# Workers run with a read-only rootfs and only /tmp plus declared artifact mounts
# writable. Redirect every library cache into ephemeral storage.
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/mpl \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba \
    TMPDIR=/tmp
WORKDIR /tmp
ENTRYPOINT ["python", "/opt/cad-agent/executor_entry.py"]
