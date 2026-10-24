FROM nexus-atrex-worker:20261018
ENV PYTHONPATH=/opt/nexus HOME=/tmp OMP_NUM_THREADS=2 MAX_JOBS=2
COPY upstream /opt/kernelfoundry
# Upstream declares Python >=3.10; delete=True is the default but its keyword
# only exists in Python 3.12. Preserve semantics on the CUDA image's Python3.10.
RUN python3 -c "from pathlib import Path; p=Path('/opt/kernelfoundry/kernelfoundry/eval_pipeline/task.py'); s=p.read_text(); assert 'delete=True' in s; p.write_text(s.replace(', delete=True', ''))"
RUN python3 -m pip install --no-cache-dir --upgrade pip 'setuptools>=77' wheel && python3 -m pip install --no-cache-dir '/opt/kernelfoundry[mcp,algo]' 'numpy<2' && python3 -m pip show kernelfoundry
COPY controller.py evaluate.py ipc.py compat.py /opt/nexus/
RUN python3 /opt/nexus/compat.py && python3 -m compileall -q /usr/local/lib/python3.10/dist-packages/kernelfoundry
ENV USER=nexus LOGNAME=nexus
WORKDIR /work
ENTRYPOINT ["python3", "-u", "/opt/nexus/controller.py"]
