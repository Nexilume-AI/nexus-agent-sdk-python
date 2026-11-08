FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git autoconf automake libtool device-tree-compiler libboost-regex-dev libboost-system-dev libgmp-dev libmpfr-dev libmpc-dev verilator yosys z3 iverilog gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/riscv-software-src/riscv-isa-sim /tmp/spike && cd /tmp/spike && git checkout 2c300cd90eb74bb79f3fcdd58596a6f8d842d01c && git submodule update --init --recursive && mkdir build && cd build && ../configure --prefix=/opt/riscv && make -j4 && make install && rm -rf /tmp/spike
COPY sby /opt/sby
RUN make -C /opt/sby install PREFIX=/usr/local
COPY upstream /opt/ava
RUN pip install --no-cache-dir -r /opt/ava/requirements.txt pytest-asyncio==0.24.0 numpy==2.1.3 && pip freeze > /opt/requirements.lock
ENV PATH=/opt/riscv/bin:$PATH
ENV PYTHONPATH=/opt/ava:/opt/ava/AGENT_B:/opt/ava/AGENT_B/ava:/opt/ava/AGENT_B/backends:/opt/ava/AGENT_C:/opt/ava/AGENT_D:/opt/ava/AGENT_E:/opt/ava/AGENT_F:/opt/ava/AGENT_G
COPY compatibility_patch.py /opt/compatibility_patch.py
RUN python /opt/compatibility_patch.py
WORKDIR /work
COPY worker.py /opt/worker.py
ENTRYPOINT ["python","/opt/worker.py"]
