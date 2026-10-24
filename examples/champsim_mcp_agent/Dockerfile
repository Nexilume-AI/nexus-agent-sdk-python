FROM nexus-ramulator-mcp:c6c7a5de0cef
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libcli11-dev nlohmann-json3-dev libfmt-dev libbz2-dev liblzma-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
COPY upstream /opt/champsim
COPY configs.json /opt/champsim/configs.json
# Ubuntu CLI11 is header-only. Use distribution dependency headers/libraries.
RUN sed -i 's/-lCLI11 //g' /opt/champsim/Makefile && cd /opt/champsim && python3 config.sh configs.json && printf '%s\n' '-I/opt/champsim/inc' > absolute.options && make -j8 TRIPLET_DIR=/usr
COPY worker.py /opt/champsim-bridge/worker.py
ENV PYTHONPATH=/opt/champsim-bridge
WORKDIR /work
ENTRYPOINT ["python3", "/opt/champsim-bridge/worker.py"]
