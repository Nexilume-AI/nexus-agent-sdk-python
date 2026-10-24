#!/bin/sh
set -eu
test ! -e upstream
curl -fL --retry 3 https://codeload.github.com/Accelergy-Project/accelforge/tar.gz/7312a7d915625552bf6eb1db577941f8317b6ac7 -o upstream.tar.gz
printf '%s  upstream.tar.gz\n' dcea939d6fcc3820000e7e57397f121ba14fe6c45e5b4d70e32e5ba19be7083f | sha256sum -c -
mkdir upstream
tar -xzf upstream.tar.gz --strip-components=1 -C upstream
