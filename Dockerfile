# Stage 1: Build React dashboard
FROM node:22-alpine AS ui-builder
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# Stage 2: Lean Python runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY backend/ ./backend/
COPY --from=ui-builder /app/dashboard/dist ./dashboard/dist
RUN uv sync --frozen --no-dev

ENV SPLITCH_DATA_DIR=/data/state \
    SPLITCH_SKILLS_DIR=/data/skills \
    SPLITCH_MCP_SCRIPTS_DIR=/data/mcp_servers \
    SPLITCH_STORAGE_PATH=/data/models

EXPOSE 8080 8090
ENTRYPOINT ["uv", "run", "--no-sync", "splitch"]
CMD ["router", "--host", "0.0.0.0", "--port", "8080"]
