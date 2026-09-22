# Build from the repository root:
#   podman build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
#
# Both bases are immutable multi-architecture OCI references. Keep them in sync
# with runtime-lock.json.
FROM docker.io/library/node@sha256:b04ce4ae4e95b522112c2e5c52f781471a5cbc3b594527bcddedee9bc48c03a0 AS node_runtime

FROM docker.io/library/python@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS freecad_runtime
ARG TARGETARCH
COPY backend/sandbox/freecad_build_verify.py /tmp/freecad_build_verify.py
RUN --mount=type=cache,target=/var/cache/freecad \
    printf 'Acquire::Check-Date "false";\n' \
      > /etc/apt/apt.conf.d/99-cad-agent-build-clock && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    case "$TARGETARCH" in \
      amd64) asset="FreeCAD_1.1.3-Linux-x86_64-py311.AppImage"; checksum="3a853eb69ee595f779f2255dbf80a765926981d8ff68903cefee4dfb03a8f5ef" ;; \
      arm64) asset="FreeCAD_1.1.3-Linux-aarch64-py311.AppImage"; checksum="9a8f9f7f2802bb856f2bb70f53d536e2ae06569f4e6d718407803076104ff55e" ;; \
      *) echo "unsupported FreeCAD architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac && \
    download="/var/cache/freecad/$asset" && \
    touch "$download" && \
    if ! echo "$checksum  $download" | sha256sum --check --status; then \
      curl --http1.1 --fail --location --retry 30 --retry-all-errors \
        --retry-delay 2 --continue-at - --output "$download" \
        "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/$asset"; \
    fi && \
    echo "$checksum  $download" | sha256sum --check --strict && \
    chmod +x "$download" && \
    cd /tmp && "$download" --appimage-extract >/dev/null && \
    mv /tmp/squashfs-root /opt/freecad && \
    mkdir -p /opt/freecad/bin && \
    freecad_cmd="$(find /opt/freecad -type f -iname 'freecadcmd' -print -quit)" && \
    test -n "$freecad_cmd" && \
    ln -s "$freecad_cmd" /opt/freecad/bin/FreeCADCmd && \
    /opt/freecad/bin/FreeCADCmd -c \
      "exec(compile(open('/tmp/freecad_build_verify.py', encoding='utf-8').read(), '/tmp/freecad_build_verify.py', 'exec'))"

FROM docker.io/library/python@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

LABEL org.opencontainers.image.title="CAD Agent unified MCAD runtime" \
      org.opencontainers.image.description="Pinned FreeCAD, CadQuery, build123d, OCP, DXF, implicit-CAD and headless snapshot worker" \
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
RUN printf 'Acquire::Check-Date "false";\n' \
      > /etc/apt/apt.conf.d/99-cad-agent-build-clock && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libgl1 libglib2.0-0 libxrender1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=freecad_runtime /opt/freecad /opt/freecad

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
COPY backend/sandbox/freecad_entry.py /opt/cad-agent/freecad_entry.py
COPY backend/sandbox/freecad_edge_scope.py /opt/cad-agent/freecad_edge_scope.py
COPY backend/sandbox/freecad_result_channel.py /opt/cad-agent/freecad_result_channel.py
COPY backend/sandbox/freecad_bom.py /opt/cad-agent/freecad_bom.py
COPY backend/sandbox/freecad_scene.py /opt/cad-agent/freecad_scene.py
COPY backend/sandbox/freecad_engineering.py /opt/cad-agent/freecad_engineering.py
COPY backend/sandbox/freecad_cam.py /opt/cad-agent/freecad_cam.py
COPY backend/sandbox/freecad_release.py /opt/cad-agent/freecad_release.py
COPY backend/app/freecad/state_projector.py /opt/cad-agent/freecad_state_projector.py
COPY backend/app/freecad/sketch_diagnostics.py /opt/cad-agent/freecad_sketch_diagnostics.py
COPY backend/app/freecad/reference_geometry.py /opt/cad-agent/freecad_reference_geometry.py
COPY backend/app/freecad/topology.py /opt/cad-agent/freecad_topology.py
COPY backend/sandbox/geometry_validation.py /opt/cad-agent/geometry_validation.py
COPY backend/sandbox/mesh_normalization.py /opt/cad-agent/mesh_normalization.py
COPY backend/sandbox/visual_render.py /opt/cad-agent/visual_render.py
COPY backend/sandbox/dfm_brep.py /opt/cad-agent/dfm_brep.py
COPY backend/sandbox/dfm_validation.py /opt/cad-agent/dfm_validation.py
COPY backend/sandbox/runtime_probe.py /opt/cad-agent/runtime_probe.py
COPY backend/sandbox/freecad_runtime_probe.py /opt/cad-agent/freecad_runtime_probe.py

RUN groupadd --gid 1000 sandbox && \
    useradd --uid 1000 --gid 1000 --no-create-home \
      --home-dir /tmp --shell /usr/sbin/nologin sandbox && \
    chown 1000:1000 /opt/cad-agent/executor_entry.py \
      /opt/cad-agent/capability_entry.py /opt/cad-agent/geometry_validation.py \
      /opt/cad-agent/freecad_entry.py /opt/cad-agent/freecad_state_projector.py \
      /opt/cad-agent/freecad_topology.py /opt/cad-agent/freecad_bom.py \
      /opt/cad-agent/visual_render.py /opt/cad-agent/dfm_validation.py \
      /opt/cad-agent/runtime_probe.py /opt/cad-agent/freecad_runtime_probe.py

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
