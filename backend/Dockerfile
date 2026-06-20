# CAD Agent backend (FastAPI). Spawns sandbox containers via the mounted docker.sock.
FROM python:3.11-slim

WORKDIR /app

# Docker CLI not required — we use the docker SDK over the mounted socket.
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY examples ./examples

# data dir for sqlite + generated files (mount a volume over this in compose)
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
