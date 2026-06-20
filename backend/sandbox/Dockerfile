FROM docker.m.daocloud.io/condaforge/miniforge3:24.3.0-0

# System libs needed by OCP (OpenCascade)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxrender1 && \
    rm -rf /var/lib/apt/lists/*

# Install CadQuery with OCP via conda-forge (supports aarch64)
RUN conda create -y -n cad python=3.11 && \
    conda run -n cad conda install -y -c conda-forge cadquery numpy trimesh && \
    conda clean -afy

# Make the conda env the default python
ENV PATH="/opt/conda/envs/cad/bin:$PATH"

# Install compatible ezdxf via pip (conda version too old for CadQuery)
RUN pip install --no-cache-dir "ezdxf>=1.0,<2.0"

# Use the existing ubuntu user (UID 1000) as sandbox user
COPY executor_entry.py /home/ubuntu/executor_entry.py
RUN chown 1000:1000 /home/ubuntu/executor_entry.py
USER 1000

# The container is run with a READ-ONLY rootfs (see executor.py hardening) and only
# /tmp is writable (tmpfs). Point all cache/scratch env at /tmp so libraries that
# expect a writable HOME/cache (matplotlib, fontconfig, numba) don't crash.
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/mpl \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba \
    TMPDIR=/tmp
WORKDIR /tmp
ENTRYPOINT ["python", "/home/ubuntu/executor_entry.py"]
