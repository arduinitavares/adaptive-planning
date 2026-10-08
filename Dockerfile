FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADAPTIVE_PLANNING_DATA_DIR=/data \
    ADAPTIVE_PLANNING_BASE_URL=http://127.0.0.1:8790

WORKDIR /app
COPY pyproject.toml uv.lock /app/
COPY adaptive_planning /app/adaptive_planning
COPY assets/dashboard /app/assets/dashboard

RUN uv sync --frozen --no-dev --no-editable \
    && install -d -m 0700 -o 10001 -g 10001 /data

USER 10001:10001
EXPOSE 8790
ENTRYPOINT ["/app/.venv/bin/adaptive-planning"]
CMD ["serve"]
