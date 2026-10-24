#!/bin/sh
set -eu
test ! -e upstream
git clone --no-checkout https://github.com/ChampSim/ChampSim.git upstream
git -C upstream checkout --detach 410cee62d9c429c043ea611325ce00e6c303be2a
