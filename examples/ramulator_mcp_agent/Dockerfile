FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONPATH=/opt/ramulator/python PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates python3 python3-dev python3-yaml && rm -rf /var/lib/apt/lists/*
COPY upstream /opt/ramulator
COPY deps /opt/deps
COPY patch_completion.py /opt/bridge/patch_completion.py
RUN python3 /opt/bridge/patch_completion.py && cmake -S /opt/ramulator -B /opt/ramulator/build -DCMAKE_BUILD_TYPE=Release -DFETCHCONTENT_SOURCE_DIR_YAML-CPP=/opt/deps/yaml-cpp -DFETCHCONTENT_SOURCE_DIR_FMT=/opt/deps/fmt -DFETCHCONTENT_SOURCE_DIR_NANOBIND=/opt/deps/nanobind && cmake --build /opt/ramulator/build --parallel 8
COPY worker.py runner.py /opt/bridge/
WORKDIR /work
ENTRYPOINT ["python3", "/opt/bridge/worker.py"]
