#!/bin/sh
# Bootstrap is user-local. Existing user and system Python installations are preserved.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_DIR=${JINGYAN_HOME:-"$HOME/.jingyan"}
# doctor must be read-only, including on a machine with no initialized venv.
if [ "${1:-}" = "doctor" ]; then
    for probe in "$DATA_DIR/venv/bin/python" "${JINGYAN_PYTHON:-}" python3 python "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3; do
        if [ -n "$probe" ] && command -v "$probe" >/dev/null 2>&1 && "$probe" -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))' >/dev/null 2>&1; then
            exec "$probe" -B "$SCRIPT_DIR/bootstrap.py" --data-dir "$DATA_DIR" --doctor
        fi
    done
    echo '{"status":"initialization_required","read_only":true,"core_ready":false,"ocr_ready":false,"model_setup_needed":true}'
    exit 0
fi
mkdir -p "$DATA_DIR"
PYTHON="$DATA_DIR/venv/bin/python"
if [ ! -x "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))' >/dev/null 2>&1; then
    CANDIDATE=${JINGYAN_PYTHON:-}
    if [ -z "$CANDIDATE" ]; then
        for executable in python3 python; do
            if command -v "$executable" >/dev/null 2>&1 && "$executable" -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))' 2>/dev/null; then
                CANDIDATE=$(command -v "$executable")
                break
            fi
        done
    fi
    if [ -z "$CANDIDATE" ]; then
        for executable in "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3; do
            if [ -x "$executable" ] && "$executable" -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))'; then
                CANDIDATE=$executable
                break
            fi
        done
    fi
    if [ -z "$CANDIDATE" ]; then
        UV=${JINGYAN_UV:-"$DATA_DIR/tools/uv"}
        if [ ! -x "$UV" ]; then
            if command -v uv >/dev/null 2>&1; then
                UV=$(command -v uv)
            else
                curl -fLsS --connect-timeout 15 --max-time 90 https://astral.sh/uv/install.sh -o "$DATA_DIR/uv-install.sh"
                UV_INSTALL_DIR="$DATA_DIR/tools" UV_NO_MODIFY_PATH=1 sh "$DATA_DIR/uv-install.sh" >&2
            fi
        fi
        export UV_PYTHON_INSTALL_DIR="$DATA_DIR/python"
        export UV_CACHE_DIR="$DATA_DIR/cache"
        if [ -L "$DATA_DIR/venv" ]; then
            echo '初始化停止：venv 路径为符号链接，请检查个人配置。' >&2
            exit 2
        fi
        if [ -e "$DATA_DIR/venv" ]; then
            mv "$DATA_DIR/venv" "$DATA_DIR/venv-incomplete-$(date +%Y%m%d-%H%M%S)-$$"
        fi
        "$UV" venv --python 3.12 "$DATA_DIR/venv" >&2
        export JINGYAN_UV="$UV"
        CANDIDATE="$DATA_DIR/venv/bin/python"
    fi
    BOOTSTRAP_PYTHON=$CANDIDATE
else
    BOOTSTRAP_PYTHON=$PYTHON
fi
CONFIG=${JINGYAN_CONFIG:-"$DATA_DIR/鲸研配置.json"}
export JINGYAN_CONFIG="$CONFIG"
if [ "$#" -eq 0 ] || [ "$1" = "init" ]; then
    if [ "$#" -gt 0 ]; then shift; fi
    exec "$BOOTSTRAP_PYTHON" "$SCRIPT_DIR/bootstrap.py" --data-dir "$DATA_DIR" "$@"
fi
"$BOOTSTRAP_PYTHON" "$SCRIPT_DIR/bootstrap.py" --data-dir "$DATA_DIR" >&2
case "$1" in
    host-task) shift; exec "$PYTHON" "$SCRIPT_DIR/host_task.py" --config "$CONFIG" "$@" ;;
    configure) shift; exec "$PYTHON" "$SCRIPT_DIR/model_setup.py" --config "$CONFIG" "$@" ;;
    reexport) shift; exec "$PYTHON" "$SCRIPT_DIR/reexport_checked.py" --config "$CONFIG" "$@" ;;
    plan-batch) shift; exec "$PYTHON" "$SCRIPT_DIR/plan_batch.py" "$@" ;;
    feishu) shift; exec "$PYTHON" "$SCRIPT_DIR/feishu_batch.py" --config "$CONFIG" "$@" ;;
    *) exec "$PYTHON" "$SCRIPT_DIR/local_reports.py" --config "$CONFIG" "$@" ;;
esac
