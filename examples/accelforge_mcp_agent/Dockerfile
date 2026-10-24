FROM nexus-accelforge-base:7312a7d91562
RUN ln -s /tmp/accelforge-cacti /opt/venv/lib/python3.12/site-packages/hwcomponents_cacti/cacti_inputs_outputs && ln -s /tmp/accelforge-neurosim /opt/venv/lib/python3.12/site-packages/hwcomponents_neurosim/neurosim_inputs_outputs
COPY worker.py runner.py /opt/accelforge-bridge/
ENV USER=nexus LOGNAME=nexus HOME=/tmp/accelforge-home XDG_CACHE_HOME=/tmp/cache NUMBA_CACHE_DIR=/tmp/numba MPLCONFIGDIR=/tmp/matplotlib OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 PYTHONPATH=/opt/accelforge
WORKDIR /work
ENTRYPOINT ["/opt/venv/bin/python", "/opt/accelforge-bridge/worker.py"]
