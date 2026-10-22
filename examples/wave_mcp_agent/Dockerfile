FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends gtkwave ca-certificates && rm -rf /var/lib/apt/lists/*
COPY upstream /opt/wave-mcp
RUN pip install --no-cache-dir /opt/wave-mcp && pip freeze > /opt/wave-requirements.lock
COPY worker.py /opt/nexus-wave-worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/work TMPDIR=/tmp WAVE_MCP_WORKERS=2 WAVE_MCP_SESSION_ROOT=/work/sessions WAVE_MCP_CACHE_ROOT=/work/cache
WORKDIR /work
ENTRYPOINT ["python", "/opt/nexus-wave-worker.py"]
