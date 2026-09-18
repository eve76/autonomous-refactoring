#!/usr/bin/env bash

# Source this file before running the experiment:
#   source ./activate_project.sh

_experiment_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_experiment_env_dir="${_experiment_dir}/../experiment_token_save/.venv"

if [[ ! -x "${_experiment_env_dir}/bin/python" ]]; then
    echo "Missing shared environment ${_experiment_env_dir}" >&2
    return 1 2>/dev/null || exit 1
fi

source "${_experiment_env_dir}/bin/activate"
export EXPERIMENT_PYTHON="${_experiment_env_dir}/bin/python"
export PATH="/usr/local/go/bin:${PATH}"
export BAZELISK_HOME="${_experiment_dir}/.cache/bazelisk"
export MPLCONFIGDIR="${_experiment_dir}/.cache/matplotlib"
export GOCACHE="${_experiment_dir}/.cache/go-build"
export GOMODCACHE="${_experiment_dir}/.cache/go-mod"
export PIP_CACHE_DIR="${_experiment_dir}/.cache/pip"

mkdir -p \
    "${BAZELISK_HOME}" \
    "${MPLCONFIGDIR}" \
    "${GOCACHE}" \
    "${GOMODCACHE}" \
    "${PIP_CACHE_DIR}"
unset _experiment_dir _experiment_env_dir
