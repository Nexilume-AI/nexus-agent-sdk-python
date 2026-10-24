FROM nexus-fastvideo-base:fa2d1324c600
COPY constraints.txt /opt/vbench-constraints.txt
RUN pip install --no-cache-dir -c /opt/vbench-constraints.txt 'numpy==1.26.4' 'timm==1.0.12' 'openai-clip==1.0.1' 'decord==0.6.0' 'omegaconf==2.3.0' easydict ftfy scipy scikit-image einops lmdb yacs facexlib addict yapf
RUN pip install --no-cache-dir --no-deps 'pyiqa==0.1.13'
COPY upstream/vbench /opt/vbench/vbench
COPY upstream/LICENSE /opt/vbench/LICENSE
COPY upstream.commit /opt/vbench/COMMIT
COPY runner.py /opt/nexus/runner.py
ENV PYTHONPATH=/opt/vbench VBENCH_CACHE_DIR=/models HOME=/tmp OMP_NUM_THREADS=4 TORCH_HOME=/tmp/torch LD_LIBRARY_PATH=/driver:/usr/local/cuda/lib64
WORKDIR /work
ENTRYPOINT ["/opt/fastvideo-env/bin/python", "/opt/nexus/runner.py"]
