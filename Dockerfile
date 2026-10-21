# syntax=docker/dockerfile:1.7
# maps-mcp: source on top of maps-mcp-deps (Dockerfile.deps). The release workflow builds
# both stages as one chain.
ARG MAPS_MCP_DEPS_IMAGE=maps-mcp-deps:latest
FROM ${MAPS_MCP_DEPS_IMAGE}

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --compile-bytecode --no-deps . \
    && rm -rf /app/src

# /state (daily budgets, breaker, Google's consent cookies) is the only writable path; run the
# container with a read-only root and a /state volume or tmpfs owned by uid 1000.
RUN mkdir -p /state && chown 1000:1000 /state

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
LABEL org.opencontainers.image.title="maps-mcp" \
      org.opencontainers.image.description="Read-only MCP server for Google Maps travel times, routes and places" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    MAPS_HOST=0.0.0.0 \
    MAPS_STATE_DIR=/state
USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["maps-mcp"]

