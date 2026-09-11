# Build from the repository root: docker build -f backend/Dockerfile .
# The runtime probe invokes the CLI; execution also uses the Docker SDK.
ARG PYTHON_IMAGE=python:3.11-slim
FROM docker:28.5.1-cli AS docker_cli
FROM ${PYTHON_IMAGE}

WORKDIR /app/backend

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker

COPY backend/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 --retries 5 -r requirements.txt

COPY backend/app ./app
COPY backend/examples ./examples
COPY backend/alembic.ini .
COPY backend/alembic ./alembic
COPY third_party/cadskills /app/third_party/cadskills

RUN mkdir -p /app/data /tmp/cad-agent-work

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
