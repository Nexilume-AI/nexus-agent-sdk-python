FROM nexus-fastvideo-base:fa2d1324c600
COPY runner.py benchmark.py train.py export.py verify_weights.py wan_dmd2.yaml /opt/nexus/
COPY h3_recipe.py h3_runner.py h3_preprocess.py h3_export.py h3_benchmark.py probe_h3.py /opt/nexus/
COPY h3_compat.py h3_train.py /opt/nexus/
ENV FASTVIDEO_ATTENTION_BACKEND=TORCH_SDPA FASTVIDEO_MINIMAX_H3_FUSIONS="" FASTVIDEO_MINIMAX_H3_EXACT_KERNELS=""
ENV LD_LIBRARY_PATH=/driver:/usr/local/cuda/lib64 HOME=/tmp USER=nexus LOGNAME=nexus PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WANDB_MODE=disabled HF_HOME=/tmp/huggingface XDG_CACHE_HOME=/tmp/cache OMP_NUM_THREADS=2
WORKDIR /work
ENTRYPOINT ["python", "-u", "/opt/nexus/runner.py"]
