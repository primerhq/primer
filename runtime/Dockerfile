# Workspace runtime base image.
#
# Build:
#   docker build -t primer/workspace-runtime:1.0 runtime/
#
# Run (minimal):
#   docker run --rm -e PRIMER_RUNTIME_TOKEN=<secret> \
#              -p 5959:5959 primer/workspace-runtime:1.0
#
# Workspace templates extend this image with:
#   FROM primer/workspace-runtime:1.0
#   RUN ...operator-specific deps...
# They must NOT override ENTRYPOINT; run custom init via init_commands after
# .runtime.ready appears.
#
# Protocol dependency:
#   runtime/primer_runtime/protocol.py is a self-contained copy — no primer
#   package needed at container runtime.  Keep it in sync with
#   primer/workspace/runtime/protocol.py on the worker side.

FROM python:3.13-slim

LABEL runtime.primer.protocol="1"
LABEL runtime.primer.version="1.0.0"

# Install runtime in its own directory so workspace files are not mixed in.
WORKDIR /opt/primer-runtime
COPY pyproject.toml ./
COPY primer_runtime/ ./primer_runtime/

# Install runtime package and its deps.  Using `pip install .` installs
# primer_runtime into site-packages so it is importable from any cwd.
# watchfiles ships with pre-built wheels for linux/amd64 and linux/arm64 so
# no Rust toolchain is required.
RUN pip install --no-cache-dir aiohttp watchfiles pydantic && \
    pip install --no-cache-dir --no-deps .

# Workspace dir — the real workspace is mounted by `docker run -v`; this
# fallback ensures the directory exists if no volume is provided.
RUN mkdir -p /workspace

# Switch cwd to /workspace so relative paths in ops resolve correctly.
WORKDIR /workspace

EXPOSE 5959

ENTRYPOINT ["python", "-m", "primer_runtime.server"]
