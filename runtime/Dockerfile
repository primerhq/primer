# Workspace runtime base image.
# Task 6 finalises this file (multi-stage build, labels, entrypoint tuning).
#
# Build:
#   docker build -t matrix/workspace-runtime:1.0 runtime/
#
# Run:
#   docker run --rm -e MATRIX_RUNTIME_TOKEN=<secret> \
#              -p 5959:5959 matrix/workspace-runtime:1.0

FROM python:3.13-slim

LABEL runtime.matrix.protocol=1
LABEL runtime.matrix.version=1.0.0

WORKDIR /app

# Install runtime deps
RUN pip install --no-cache-dir aiohttp aionotify pydantic

# Copy runtime package (matrix package installed separately in Task 6 image)
COPY matrix_runtime/ ./matrix_runtime/
COPY pyproject.toml ./

# Workspace mount point
RUN mkdir -p /workspace

EXPOSE 5959

ENTRYPOINT ["python", "-m", "matrix_runtime.server"]
