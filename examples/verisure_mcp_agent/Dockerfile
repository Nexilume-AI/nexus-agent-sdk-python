FROM nexus-ava-mcp:d247b29c8399
ENV PYTHONPATH=/opt/verisure HOME=/tmp YOWASP_CACHE_DIR=/opt/yowasp-cache
COPY upstream /opt/verisure
RUN pip install --no-cache-dir -r /opt/verisure/requirements.txt yowasp-yosys && pip freeze > /opt/requirements.lock
RUN ln -sf /usr/local/bin/yowasp-yosys /usr/local/bin/yosys && ln -sf /usr/local/bin/yowasp-yosys-smtbmc /usr/local/bin/yosys-smtbmc
RUN yosys -V && chmod -R a+rX /opt/yowasp-cache
COPY compatibility_patch.py /opt/verisure-compatibility.py
RUN python /opt/verisure-compatibility.py
WORKDIR /work
COPY worker.py /opt/verisure-worker.py
ENTRYPOINT ["python", "/opt/verisure-worker.py"]
