FROM python:3.12-slim

WORKDIR /app

COPY examples/hosted_interactive_display_agent/requirements.txt /tmp/requirements.txt
COPY dist/nexus_openwrt_agent_sdk-0.27.0-py3-none-any.whl /tmp/wheels/
RUN pip install --no-cache-dir -r /tmp/requirements.txt /tmp/wheels/nexus_openwrt_agent_sdk-0.27.0-py3-none-any.whl \
    && rm -rf /tmp/wheels /tmp/requirements.txt

COPY examples/hosted_interactive_display_agent/agent.py /app/agent.py

EXPOSE 8000
CMD ["python", "-B", "/app/agent.py"]
