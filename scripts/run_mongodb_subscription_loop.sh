#!/usr/bin/env bash

# Supervise one fresh MongoDB query refactoring run.  The experiment remains
# the authority for quota detection and resume safety; this script only
# restarts a cleanly paused subscription run after the configured reset wait.

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly EXPERIMENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly RUN_ROOT="${EXPERIMENT_DIR}/production_runs/mongodb-query"
readonly RESULTS_ROOT="${RUN_ROOT}/results"
readonly WORKTREES_ROOT="${RUN_ROOT}/worktrees"
readonly REPO_ROOT="$(cd "${EXPERIMENT_DIR}/../../dev/mongo" && pwd)"
readonly BASELINE_COMMIT="fbb28cf8c44023d334a646fe496fb95d355dc6f0"
readonly MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-120}"
readonly QUOTA_WAIT_SEC="${QUOTA_WAIT_SEC:-18300}"
readonly MIN_AVAILABLE_MEM_KB="${MIN_AVAILABLE_MEM_KB:-8388608}"
readonly MIN_FREE_DISK_KB="${MIN_FREE_DISK_KB:-104857600}"
readonly RESUME_PROVIDER="${RESUME_PROVIDER:-subscription}"

run_id=""
preflight_only=0
dry_run=0
resume_existing=0
child_pid=""

usage() {
    cat <<'EOF'
Usage: run_mongodb_subscription_loop.sh [options]

Options:
  --run-id ID        Fresh run identifier (default: timestamped Opus 5 ID)
  --resume            Resume an existing, safely quota-paused run
  --preflight-only   Check the environment without starting an experiment
  --dry-run          Print the fresh-run command after successful preflight
  -h, --help         Show this help

Environment:
  MONITOR_INTERVAL_SEC  Running-state poll interval (default: 120)
  QUOTA_WAIT_SEC        Wait after subscription quota pause (default: 18300)
  RESUME_PROVIDER       subscription (default) or openrouter
  MIN_AVAILABLE_MEM_KB  Startup/resume memory floor (default: 8388608)
  MIN_FREE_DISK_KB      Fresh-run disk floor (default: 104857600)
EOF
}

log() {
    printf '[%(%F %T %Z)T] %s\n' -1 "$*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --run-id)
            (($# >= 2)) || die "--run-id requires a value"
            run_id="$2"
            shift 2
            ;;
        --preflight-only)
            preflight_only=1
            shift
            ;;
        --resume)
            resume_existing=1
            shift
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

[[ "${MONITOR_INTERVAL_SEC}" =~ ^[1-9][0-9]*$ ]] || die "invalid MONITOR_INTERVAL_SEC"
[[ "${QUOTA_WAIT_SEC}" =~ ^[1-9][0-9]*$ ]] || die "invalid QUOTA_WAIT_SEC"
[[ "${MIN_AVAILABLE_MEM_KB}" =~ ^[1-9][0-9]*$ ]] || die "invalid MIN_AVAILABLE_MEM_KB"
[[ "${MIN_FREE_DISK_KB}" =~ ^[1-9][0-9]*$ ]] || die "invalid MIN_FREE_DISK_KB"
[[ "${RESUME_PROVIDER}" == "subscription" || "${RESUME_PROVIDER}" == "openrouter" ]] \
    || die "RESUME_PROVIDER must be subscription or openrouter"

if [[ -z "${run_id}" ]]; then
    run_id="mongodb_query_static_opus5_$(date +%Y%m%d_%H%M%S)_auto"
fi
[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || die "run ID contains unsafe characters: ${run_id}"

mkdir -p "${RUN_ROOT}" "${RESULTS_ROOT}" "${WORKTREES_ROOT}"
exec 9>"${RUN_ROOT}/supervisor.lock"
flock -n 9 || die "another MongoDB experiment supervisor already holds the lock"

cd "${EXPERIMENT_DIR}"
# Required by the repository instructions.  Every fresh/resume child inherits
# this exact activated environment; the .venv Python is never invoked directly.
# shellcheck source=../activate_project.sh
source ./activate_project.sh

readonly RESULTS_DIR="${RESULTS_ROOT}/${run_id}"
readonly STATE_FILE="${RESULTS_DIR}/run_state.json"
readonly SUMMARY_FILE="${RESULTS_DIR}/run_summary.json"
readonly INTEGRATION_BRANCH="refactor/${run_id}"

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

check_python_environment() {
    [[ -n "${EXPERIMENT_PYTHON:-}" ]] || die "activate_project.sh did not set EXPERIMENT_PYTHON"
    local active_python configured_python
    active_python="$(readlink -f "$(command -v python)")"
    configured_python="$(readlink -f "${EXPERIMENT_PYTHON}")"
    [[ "${active_python}" == "${configured_python}" ]] \
        || die "python does not match the activated experiment environment"
    python -c 'from pathlib import Path; from config import Config; from production_profiles import get_production_profile; c=Config(repo_root=Path("."), target_subdir=".", work_root=Path("."), api_provider="subscription"); p=get_production_profile("mongodb-query"); assert c.orchestrator_model == "claude-opus-5"; assert c.agent_model == "claude-opus-5"; assert p.target_subdir == "src/mongo/db/query"; assert p.serialize_merge_gate is True'
}

check_subscription_auth() {
    local auth_json
    if ! auth_json="$(claude auth status --json 2>/dev/null)"; then
        die "Claude subscription authentication check failed"
    fi
    jq -e '.loggedIn == true and (.subscriptionType | type == "string" and length > 0)' \
        >/dev/null <<<"${auth_json}" \
        || die "Claude Code is not logged in with a subscription"
}

check_openrouter_auth() {
    [[ -n "${OPENROUTER_API_KEY:-}" ]] \
        || die "RESUME_PROVIDER=openrouter but OPENROUTER_API_KEY is unset"
}

check_repo_clean() {
    git -C "${REPO_ROOT}" diff --quiet -- || die "MongoDB checkout has tracked changes"
    git -C "${REPO_ROOT}" diff --cached --quiet -- \
        || die "MongoDB checkout has staged changes"
    local path
    while IFS= read -r path; do
        case "${path}" in
            .venv|.venv/*|experiment_logs|experiment_logs/*|MODULE.bazel.lock|activate_mongo_env.sh)
                ;;
            *)
                die "MongoDB checkout has unsupported untracked path: ${path}"
                ;;
        esac
    done < <(git -C "${REPO_ROOT}" ls-files --others --exclude-standard)
    git -C "${REPO_ROOT}" cat-file -e "${BASELINE_COMMIT}^{commit}" \
        || die "configured MongoDB baseline commit is unavailable"
}

check_resources() {
    local available_mem_kb free_disk_kb
    available_mem_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
    free_disk_kb="$(df -Pk "${RUN_ROOT}" | awk 'NR == 2 {print $4}')"
    ((available_mem_kb >= MIN_AVAILABLE_MEM_KB)) \
        || die "available memory ${available_mem_kb} KiB is below ${MIN_AVAILABLE_MEM_KB} KiB"
    ((free_disk_kb >= MIN_FREE_DISK_KB)) \
        || die "free disk ${free_disk_kb} KiB is below ${MIN_FREE_DISK_KB} KiB"
}

check_fresh_state() {
    [[ ! -e "${RESULTS_DIR}" ]] || die "fresh result path already exists: ${RESULTS_DIR}"
    [[ ! -e "${WORKTREES_ROOT}/${run_id}" ]] \
        || die "fresh worktree path already exists for ${run_id}"
    ! git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${INTEGRATION_BRANCH}" \
        || die "fresh integration branch already exists: ${INTEGRATION_BRANCH}"
    if [[ -d "${RUN_ROOT}/bazel_disk_cache" ]]; then
        [[ -z "$(find "${RUN_ROOT}/bazel_disk_cache" -mindepth 1 -print -quit)" ]] \
            || die "MongoDB Bazel disk cache is not empty; refusing old-cache reuse"
    fi
}

check_resume_state() {
    local expected_reason="$1"
    local provider="$2"
    [[ -s "${STATE_FILE}" ]] || die "resume state is missing: ${STATE_FILE}"
    jq -e --arg id "${run_id}" --arg reason "${expected_reason}" \
        '.run_id == $id and .stop_reason == $reason and
         (.optimization_fingerprint | type == "string" and length > 0) and
         (.optimization_core_fingerprint | type == "string" and length > 0) and
         (.baseline_commit | type == "string" and length > 0)' \
        "${STATE_FILE}" >/dev/null \
        || die "run state is not eligible for the expected safe resume"
    git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${INTEGRATION_BRANCH}" \
        || die "resume integration branch is missing: ${INTEGRATION_BRANCH}"
    [[ ! -d "${WORKTREES_ROOT}/${run_id}" || \
       -z "$(find "${WORKTREES_ROOT}/${run_id}" -mindepth 1 -print -quit)" ]] \
        || die "run worktrees remain before resume: ${WORKTREES_ROOT}/${run_id}"
    check_repo_clean
    check_resources
    if [[ "${provider}" == "subscription" ]]; then
        check_subscription_auth
    else
        check_openrouter_auth
    fi
}

report_status() {
    local phase="$1"
    local state_summary="state=not-yet-written"
    if [[ -s "${STATE_FILE}" ]] && jq -e . "${STATE_FILE}" >/dev/null 2>&1; then
        state_summary="$(jq -r '["penalty=" + (.current_penalty|tostring), "merges=" + (.merges|tostring), "stagnation=" + (.stagnation_counter|tostring), "stop=" + (if .stop_reason == "" then "running" else .stop_reason end), "updated=" + (.updated_at|tostring)] | join(" ")' "${STATE_FILE}")"
    fi
    local gate_summary="gates=none"
    if compgen -G "${RESULTS_DIR}/gate_status/*.json" >/dev/null; then
        gate_summary="gates=$(jq -rs 'map((.agent // "unknown") + ":" + (.phase // "unknown")) | join(",")' "${RESULTS_DIR}"/gate_status/*.json 2>/dev/null || printf unreadable)"
    fi
    local available_mem_kb free_disk_kb
    available_mem_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
    free_disk_kb="$(df -Pk "${RUN_ROOT}" | awk 'NR == 2 {print $4}')"
    log "${phase} pid=${child_pid:-none} ${state_summary} ${gate_summary} mem_available_kib=${available_mem_kb} disk_free_kib=${free_disk_kb}"
}

terminate_child() {
    if [[ -n "${child_pid}" ]] && kill -0 "${child_pid}" 2>/dev/null; then
        log "supervisor received a termination signal; forwarding SIGTERM to experiment pid=${child_pid}"
        kill -TERM "${child_pid}" 2>/dev/null || true
        wait "${child_pid}" 2>/dev/null || true
    fi
    exit 143
}
trap terminate_child TERM INT HUP

run_experiment() {
    local provider="$1"
    local mode="$2"
    local -a command=(python main.py --profile mongodb-query --provider "${provider}" --run-id "${run_id}")
    if [[ "${mode}" == "resume" ]]; then
        command+=(--resume)
    fi
    local resume_label=""
    [[ "${mode}" == "resume" ]] && resume_label=" --resume"
    log "launching: python main.py --profile mongodb-query --provider ${provider} --run-id ${run_id}${resume_label}"
    "${command[@]}" &
    child_pid=$!
    while kill -0 "${child_pid}" 2>/dev/null; do
        report_status "monitor"
        sleep "${MONITOR_INTERVAL_SEC}"
    done
    local rc=0
    wait "${child_pid}" || rc=$?
    child_pid=""
    report_status "exited rc=${rc}"
    return "${rc}"
}

for command_name in python jq git claude flock awk df find; do
    require_command "${command_name}"
done
check_python_environment
check_subscription_auth
check_repo_clean
if ((resume_existing)); then
    check_resume_state "subscription_quota_exhausted" "subscription"
else
    check_resources
    check_fresh_state
fi

log "preflight passed: run_id=${run_id} initial_mode=$([[ ${resume_existing} -eq 1 ]] && printf resume || printf fresh) model=claude-opus-5 scope=src/mongo/db/query monitor=${MONITOR_INTERVAL_SEC}s quota_wait=${QUOTA_WAIT_SEC}s resume_provider=${RESUME_PROVIDER}"
if ((preflight_only)); then
    exit 0
fi
if ((dry_run)); then
    log "dry run complete; no experiment was started"
    exit 0
fi

current_provider="subscription"
mode="fresh"
((resume_existing)) && mode="resume"
while true; do
    rc=0
    run_experiment "${current_provider}" "${mode}" || rc=$?
    [[ -s "${STATE_FILE}" ]] || die "experiment exited rc=${rc} without a valid run state"
    jq -e . "${STATE_FILE}" >/dev/null || die "experiment exited with corrupt run state"
    stop_reason="$(jq -r '.stop_reason // ""' "${STATE_FILE}")"

    case "${stop_reason}" in
        subscription_quota_exhausted)
            [[ "${current_provider}" == "subscription" ]] \
                || die "OpenRouter run reported a subscription-only quota reason"
            log "subscription quota pause confirmed; waiting ${QUOTA_WAIT_SEC}s (5h05m)"
            sleep "${QUOTA_WAIT_SEC}"
            current_provider="${RESUME_PROVIDER}"
            check_resume_state "subscription_quota_exhausted" "${current_provider}"
            log "safe-resume preflight passed for provider=${current_provider}"
            mode="resume"
            ;;
        stagnation|penalty_zero|no_actionable_work|token_budget_*|cost_budget_*)
            log "experiment completed with terminal stop_reason=${stop_reason} rc=${rc}"
            exit 0
            ;;
        wall_timeout)
            die "unexpected wall_timeout; supervisor did not initiate a resumable segment timeout"
            ;;
        orchestrator_no_progress)
            die "orchestrator made no progress; manual diagnosis required"
            ;;
        baseline_build_failed|baseline_build_timeout)
            die "baseline validation failed: ${stop_reason}"
            ;;
        "")
            die "experiment exited rc=${rc} without a terminal stop reason"
            ;;
        *)
            die "experiment stopped with unrecognized reason=${stop_reason} rc=${rc}"
            ;;
    esac
done
