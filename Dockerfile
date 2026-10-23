FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    MADAR_BIND=0.0.0.0 MADAR_DATA_DIR=/app/data WALLEX_DASHBOARD_PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 madar \
    && useradd --uid 10001 --gid madar --no-create-home madar \
    && mkdir -p /app/data && chown madar:madar /app/data
COPY --chown=madar:madar engine.py server.py wallex_settings.py mcp_link.py wallex_mcp.py llm_gateway.py ta_indicators.py challenge_rules.py competition.py product_status.py live_order.py ./
COPY --chown=madar:madar static/ ./static/
COPY --chown=madar:madar rules/ ./rules/
COPY --chown=madar:madar release/ ./release/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3).close()"
CMD ["python", "server.py"]

FROM base AS research
USER root
COPY requirements-research.txt ./
RUN pip install --no-cache-dir -r requirements-research.txt
COPY --chown=madar:madar hybrid_research.py run_hybrid_research.py challenge_research.py freeze_research_history.py backtest.py ./
USER 10001:10001

FROM base AS production
