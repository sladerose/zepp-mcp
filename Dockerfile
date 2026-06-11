FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN uv pip install "zepp-life-mcp @ git+https://github.com/kubulashvili/zepp-life-mcp.git" "mcp[cli]>=1.0.0"

# Copy the SSE wrapper server
COPY server.py ./

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
