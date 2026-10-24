FROM python:3.12-slim
COPY upstream /opt/openevolve
RUN pip install --no-cache-dir /opt/openevolve
COPY runner.py ipc_client.py evaluator_bridge.py evaluate.py /opt/nexus/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
WORKDIR /work
ENTRYPOINT ["python", "-u", "/opt/nexus/runner.py"]
