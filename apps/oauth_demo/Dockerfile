FROM python:3.12-alpine

COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv

WORKDIR /app
COPY . .
RUN uv sync

EXPOSE 8080

CMD ["uv", "run", "hypercorn", "-b", "0.0.0.0:8080", "oauth_demo.app:app"]
