#!/bin/sh
set -eu
test ! -e upstream
curl -fL --retry 3 https://codeload.github.com/sstsimulator/sst-core/tar.gz/ffd71527bc26a1f2a4fb685fdeb2ca0799997d78 -o upstream.tar.gz
printf '%s  upstream.tar.gz\n' 88b3c86063ae13707b240138a040a0e245cb6fca5ea5d9d78e77516231915f64 | sha256sum -c -
mkdir upstream
tar -xzf upstream.tar.gz --strip-components=1 -C upstream
test ! -e elements
curl -fL --retry 3 https://codeload.github.com/sstsimulator/sst-elements/tar.gz/242b48a510ead00ac775a6d80c8c5d9bb708aa8d -o elements.tar.gz
printf '%s  elements.tar.gz\n' b707595a8b8f5ef61603e44ec1de065ae8b64c5fc2c7a9d5ad0be88f55a21223 | sha256sum -c -
mkdir elements
tar -xzf elements.tar.gz --strip-components=1 -C elements
