#!/system/bin/sh
# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -eu

MCLAW_ROOT=${MCLAW_ROOT:-/data/local/release/opt/mclaw-main}
MCLAW_HOME=${MCLAW_HOME:-/data/local/tmp/.mclaw}
MCLAW_SITE_PACKAGES=/data/local/release/usr/lib/python3.12/site-packages
MCLAW_PYTHON_PRELOAD=/data/local/release/usr/lib/libpython3.12.so.1.0

export MCLAW_HOME
if [ -n "${PYTHONPATH:-}" ]; then
    export PYTHONPATH="${MCLAW_ROOT}:${MCLAW_SITE_PACKAGES}:${PYTHONPATH}"
else
    export PYTHONPATH="${MCLAW_ROOT}:${MCLAW_SITE_PACKAGES}"
fi
export LD_PRELOAD="${MCLAW_PYTHON_PRELOAD}"

exec python3 -m mclaw.cli.main "$@"
