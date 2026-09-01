FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY control-plane ./control-plane
RUN --mount=type=cache,target=/root/.cache/pip pip install .
USER 65532:65532
ENTRYPOINT ["agent-fabric-api"]

FROM runtime AS test
USER root
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[dev]"
ENTRYPOINT ["python", "-m", "pytest"]
