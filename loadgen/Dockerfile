FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY control-plane ./control-plane
RUN --mount=type=cache,target=/root/.cache/pip pip install .
USER 65532:65532
ENTRYPOINT ["agent-fabric-loadgen"]
