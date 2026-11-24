# Single OCI image for API and Worker. Run as non-root.
FROM python:3.12-slim AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md AGENTS.md ./
COPY dataagent ./dataagent
COPY packages/dataagent-sdk ./packages/dataagent-sdk
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir -e . -e ./packages/dataagent-sdk

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m -s /usr/sbin/nologin dataagent
WORKDIR /app
COPY --from=build /usr/local /usr/local
COPY --from=build /src /app
USER 10001:10001
ENV PYTHONUNBUFFERED=1 \
    DATAAGENT_ENVIRONMENT=production \
    DATAAGENT_AUTH_MODE=oidc
EXPOSE 8000
# Entry points selected via command: api | worker | enqueue-daily | migrate | smoke
ENTRYPOINT ["dataagent"]
CMD ["api"]
