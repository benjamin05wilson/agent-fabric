FROM python:3.12.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv
ENV PATH="/app/.venv/bin:$PATH" UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY control-plane ./control-plane
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
USER 65532:65532
ENTRYPOINT ["agent-fabric-loadgen"]
