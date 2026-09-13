FROM python:3.12-slim

WORKDIR /app

COPY examples/hosted_caller_mobile_agent/requirements.txt /tmp/requirements.txt
COPY dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir -r /tmp/requirements.txt /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels /tmp/requirements.txt

COPY examples/hosted_caller_mobile_agent/agent.py /app/agent.py

EXPOSE 8000
CMD ["python", "-B", "/app/agent.py"]
