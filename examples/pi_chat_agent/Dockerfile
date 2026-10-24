FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 bash git ripgrep ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/pi
RUN npm init -y && npm install --ignore-scripts --save-exact @earendil-works/pi-coding-agent@1.4.0 undici@8.10.2
COPY engine.mjs tools.mjs /opt/pi/
ENV HOME=/tmp PI_CODING_AGENT_DIR=/tmp/pi-agent
WORKDIR /workspace
ENTRYPOINT ["node", "/opt/pi/engine.mjs"]
