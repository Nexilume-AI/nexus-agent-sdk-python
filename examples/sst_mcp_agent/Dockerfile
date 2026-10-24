FROM nexus-sst-base:16.0.0 AS artifacts
RUN cp -a /opt/sst /opt/sst-runtime && python3 -c "from pathlib import Path; import subprocess; [(subprocess.run(['strip','--strip-unneeded',str(p)],check=True)) for p in Path('/opt/sst-runtime').rglob('*') if p.is_file() and not p.is_symlink() and p.open('rb').read(4)==bytes([127,69,76,70])]"
FROM nexus-ramulator-mcp:c6c7a5de0cef
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends autoconf automake libtool libtool-bin openmpi-bin libopenmpi-dev zlib1g-dev python3-dev && rm -rf /var/lib/apt/lists/*
COPY --from=artifacts /opt/sst-runtime /opt/sst
ENV PATH=/opt/sst/bin:$PATH HOME=/tmp USER=nexus LOGNAME=nexus PYTHONUNBUFFERED=1
COPY worker.py /opt/nexus/worker.py
COPY examples /opt/nexus/examples
WORKDIR /work
ENTRYPOINT ["python3", "-u", "/opt/nexus/worker.py"]
