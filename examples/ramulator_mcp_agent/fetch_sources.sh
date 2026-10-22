#!/bin/sh
set -eu
test ! -e upstream
test ! -e deps
git clone --no-checkout https://github.com/CMU-SAFARI/ramulator2.git upstream
git -C upstream checkout --detach c6c7a5de0cef35935d6fad7369e6271406798865
mkdir -p deps/yaml-cpp deps/fmt deps/nanobind
curl -fL --retry 2 https://codeload.github.com/jbeder/yaml-cpp/tar.gz/refs/tags/yaml-cpp-0.9.0 -o deps/yaml-cpp.tar.gz
curl -fL --retry 2 https://codeload.github.com/fmtlib/fmt/tar.gz/refs/tags/10.2.1 -o deps/fmt.tar.gz
curl -fL --retry 2 https://codeload.github.com/wjakob/nanobind/tar.gz/refs/tags/v2.4.0 -o deps/nanobind.tar.gz
curl -fL --retry 2 https://codeload.github.com/Tessil/robin-map/tar.gz/188c45569cc2a5dd768077c193830b51d33a5020 -o deps/robin-map.tar.gz
tar -xzf deps/yaml-cpp.tar.gz --strip-components=1 -C deps/yaml-cpp
tar -xzf deps/fmt.tar.gz --strip-components=1 -C deps/fmt
tar -xzf deps/nanobind.tar.gz --strip-components=1 -C deps/nanobind
tar -xzf deps/robin-map.tar.gz --strip-components=1 -C deps/nanobind/ext/robin_map
sha256sum deps/*.tar.gz > deps/SHA256SUMS
