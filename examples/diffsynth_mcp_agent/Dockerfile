FROM nexus-fastvideo-base:fa2d1324c600
RUN pip install --no-cache-dir --no-deps modelscope==1.35.4
COPY upstream /opt/diffsynth
RUN pip install --no-deps --no-build-isolation /opt/diffsynth
COPY runner.py /opt/nexus/runner.py
ENV PYTHONPATH=/opt/diffsynth HOME=/tmp OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 LD_LIBRARY_PATH=/driver:/usr/local/cuda/lib64
WORKDIR /work
ENTRYPOINT ["/opt/fastvideo-env/bin/python", "/opt/nexus/runner.py"]
