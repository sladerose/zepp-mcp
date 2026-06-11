FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

# Install zepp-life-mcp and its MCP dependency
RUN uv pip install zepp-life-mcp "mcp[cli]>=1.0.0"

# Copy the SSE wrapper server
COPY server.py ./

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
