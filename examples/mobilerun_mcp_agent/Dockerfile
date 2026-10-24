FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/home \
    ANONYMIZED_TELEMETRY=false MOBILERUN_TELEMETRY_ENABLED=false
WORKDIR /opt/mobilerun
COPY upstream /opt/upstream
RUN pip install --no-cache-dir /opt/upstream cryptography websockets jsonschema
COPY sdk /opt/sdk
COPY adapter /opt/mobilerun
ENV PYTHONPATH=/opt/sdk
USER 1000:1000
ENTRYPOINT ["python", "agent.py"]
