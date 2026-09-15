#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-/home/zhangyuan/projects/WHartTest}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/data/dev-logs}"
PID_DIR="${PID_DIR:-$ROOT_DIR/data/dev-pids}"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
INFRA_SERVICES="${INFRA_SERVICES:-postgres redis qdrant playwright-mcp}"

START_INFRA="${START_INFRA:-0}"
START_BACKEND="${START_BACKEND:-1}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"
START_CELERY="${START_CELERY:-1}"
START_MCP="${START_MCP:-1}"
START_FRONTEND="${START_FRONTEND:-1}"
START_ACTUATOR="${START_ACTUATOR:-1}"

RESTART_BACKEND="${RESTART_BACKEND:-0}"

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

DJANGO_DIR="$ROOT_DIR/WHartTest_Django"
VUE_DIR="$ROOT_DIR/WHartTest_Vue"
MCP_DIR="$ROOT_DIR/WHartTest_MCP"
ACTUATOR_DIR="$ROOT_DIR/WHartTest_Actuator"

DJANGO_PYTHON="${DJANGO_PYTHON:-$DJANGO_DIR/.venv/bin/python}"
DJANGO_UVICORN="${DJANGO_UVICORN:-$DJANGO_DIR/.venv/bin/uvicorn}"
DJANGO_CELERY="${DJANGO_CELERY:-$DJANGO_DIR/.venv/bin/celery}"
MCP_PYTHON="${MCP_PYTHON:-$MCP_DIR/.venv/bin/python}"
ACTUATOR_PYTHON="${ACTUATOR_PYTHON:-$ACTUATOR_DIR/.venv/bin/python}"
NPM_BIN="${NPM_BIN:-npm}"

COMPOSE_CMD=()

usage() {
  cat <<'USAGE'
用法:
  ./start_wharttest.sh

本脚本按本地开发方式启动：
  - 默认假设 Postgres、Redis、Qdrant 等基础设施已自启动
  - 只从本地源码环境后台启动 Django、Celery、MCP 工具、Vue、执行器
  - 如确实需要脚本启动基础设施，可设置 START_INFRA=1

常用环境变量:
  START_INFRA=0|1       是否启动基础设施容器，默认 0
  START_BACKEND=0|1     是否启动 Django 后端，默认 1
  RUN_MIGRATIONS=0|1    是否启动前执行 Django migrate，默认 0
  START_CELERY=0|1      是否启动 Celery worker/beat，默认 1
  START_MCP=0|1         是否启动本地 MCP 工具服务，默认 1
  START_FRONTEND=0|1    是否启动 Vue 前端，默认 1
  START_ACTUATOR=0|1    是否启动 UI 执行器，默认 1
  RESTART_BACKEND=0|1   是否强制重启 Django 后端，默认 0
  BACKEND_PORT=8000     Django 后端端口
  FRONTEND_PORT=5173    Vue 前端端口

示例:
  START_ACTUATOR=0 ./start_wharttest.sh
  RESTART_BACKEND=1 START_CELERY=0 START_MCP=0 START_FRONTEND=0 START_ACTUATOR=0 ./start_wharttest.sh
  START_INFRA=0 RUN_MIGRATIONS=0 ./start_wharttest.sh
USAGE
}

detect_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return 0
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return 0
  fi

  echo "未找到 docker compose 或 docker-compose。" >&2
  exit 1
}

ensure_executable() {
  local path="$1"
  local label="$2"
  if [ ! -x "$path" ]; then
    echo "$label 不存在或不可执行: $path" >&2
    exit 1
  fi
}

pid_matches_service() {
  local pid="$1"
  local cwd="$2"
  local actual_cwd

  if [ -z "$pid" ] || ! kill -0 "$pid" >/dev/null 2>&1; then
    return 1
  fi

  actual_cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
  [ "$actual_cwd" = "$cwd" ]
}

port_is_listening() {
  local port="$1"
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
}

wait_for_http() {
  local url="$1"
  local timeout="$2"
  local elapsed=0
  local status_code

  while [ "$elapsed" -lt "$timeout" ]; do
    status_code="$(curl -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [ -n "$status_code" ] && [ "$status_code" -ge 200 ] && [ "$status_code" -lt 500 ]; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  return 1
}

wait_for_actuator() {
  local timeout="$1"
  local elapsed=0
  local response

  while [ "$elapsed" -lt "$timeout" ]; do
    response="$(curl -sS "http://127.0.0.1:$BACKEND_PORT/api/ui-automation/actuators/list_actuators/" 2>/dev/null || true)"
    if printf '%s' "$response" | grep -Eq '"count"[[:space:]]*:[[:space:]]*[1-9]'; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  return 1
}

load_env_and_exec() {
  local env_file="$1"
  shift

  if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi

  exec "$@"
}

start_background() {
  local name="$1"
  local cwd="$2"
  local env_file="$3"
  local log_file="$4"
  local pid_file="$5"
  shift 5

  if [ -f "$pid_file" ]; then
    local old_pid
    old_pid="$(cat "$pid_file" 2>/dev/null || true)"
    if pid_matches_service "$old_pid" "$cwd"; then
      echo "$name 已在运行，PID: $old_pid"
      return 0
    fi
  fi

  echo "启动 $name..."
  (
    cd "$cwd"
    if [ -f "$env_file" ]; then
      set -a
      # shellcheck disable=SC1090
      source "$env_file"
      set +a
    fi
    setsid nohup "$@" > "$log_file" 2>&1 < /dev/null &
    echo $! > "$pid_file"
  )
  echo "$name PID: $(cat "$pid_file")，日志: $log_file"
}

stop_background() {
  local name="$1"
  local cwd="$2"
  local pid_file="$3"
  local timeout="${4:-20}"

  if [ ! -f "$pid_file" ]; then
    echo "$name 没有 PID 文件，跳过停止。"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if ! pid_matches_service "$pid" "$cwd"; then
    echo "$name PID 文件已失效，移除: $pid_file"
    rm -f "$pid_file"
    return 0
  fi

  echo "停止 $name，PID: $pid..."
  kill "$pid" >/dev/null 2>&1 || true

  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$pid_file"
      echo "$name 已停止。"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  echo "$name 未在 ${timeout}s 内停止，强制结束 PID: $pid"
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$pid_file"
}

start_infra() {
  detect_compose
  echo "启动本地开发基础设施容器: $INFRA_SERVICES"
  "${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" up -d $INFRA_SERVICES
}

run_migrations() {
  ensure_executable "$DJANGO_PYTHON" "Django Python"
  echo "执行 Django 数据库迁移..."
  (
    cd "$DJANGO_DIR"
    if [ -f .env ]; then
      set -a
      # shellcheck disable=SC1091
      source .env
      set +a
    fi
    "$DJANGO_PYTHON" manage.py migrate --noinput
  )
}

start_backend() {
  ensure_executable "$DJANGO_UVICORN" "uvicorn"
  if [ "$RESTART_BACKEND" = "1" ]; then
    stop_background "Django 后端" "$DJANGO_DIR" "$PID_DIR/backend.pid"
  fi

  if port_is_listening "$BACKEND_PORT"; then
    if wait_for_http "http://127.0.0.1:$BACKEND_PORT/api/" 4; then
      echo "Django 端口 $BACKEND_PORT 已有可用服务，跳过后端启动。"
      return 0
    fi
    echo "Django 端口 $BACKEND_PORT 已被占用，但健康检查未通过。" >&2
    echo "请检查占用该端口的进程后重试。" >&2
    exit 1
  fi

  start_background \
    "Django 后端" \
    "$DJANGO_DIR" \
    "$DJANGO_DIR/.env" \
    "$LOG_DIR/backend.log" \
    "$PID_DIR/backend.pid" \
    "$DJANGO_UVICORN" wharttest_django.asgi:application --host "$BACKEND_HOST" --port "$BACKEND_PORT"

  echo "等待 Django 后端..."
  if ! wait_for_http "http://127.0.0.1:$BACKEND_PORT/api/" "$WAIT_TIMEOUT"; then
    echo "Django 后端健康检查超时，请查看: $LOG_DIR/backend.log" >&2
    exit 1
  fi
}

start_celery() {
  ensure_executable "$DJANGO_CELERY" "celery"
  start_background \
    "Celery Worker" \
    "$DJANGO_DIR" \
    "$DJANGO_DIR/.env" \
    "$LOG_DIR/celery-worker.log" \
    "$PID_DIR/celery-worker.pid" \
    "$DJANGO_CELERY" -A wharttest_django worker -l info --concurrency=4 -Q celery,task_center

  start_background \
    "Celery Beat" \
    "$DJANGO_DIR" \
    "$DJANGO_DIR/.env" \
    "$LOG_DIR/celery-beat.log" \
    "$PID_DIR/celery-beat.pid" \
    "$DJANGO_CELERY" -A wharttest_django beat -l info --schedule="$ROOT_DIR/data/celerybeat-schedule"
}

start_mcp() {
  ensure_executable "$MCP_PYTHON" "MCP Python"

  if port_is_listening 8006; then
    echo "MCP 端口 8006 已被占用，跳过 WHartTest_tools。"
  else
    start_background \
      "WHartTest MCP 工具" \
      "$MCP_DIR" \
      "$MCP_DIR/.env" \
      "$LOG_DIR/mcp-wharttest-tools.log" \
      "$PID_DIR/mcp-wharttest-tools.pid" \
      "$MCP_PYTHON" WHartTest_tools.py
  fi

  if port_is_listening 8007; then
    echo "MCP 端口 8007 已被占用，跳过 ms_mcp_api。"
  else
    start_background \
      "MS MCP 工具" \
      "$MCP_DIR" \
      "$MCP_DIR/.env" \
      "$LOG_DIR/mcp-ms-api.log" \
      "$PID_DIR/mcp-ms-api.pid" \
      "$MCP_PYTHON" ms_mcp_api.py
  fi
}

start_frontend() {
  if ! command -v "$NPM_BIN" >/dev/null 2>&1; then
    echo "未找到 npm，无法启动前端。" >&2
    exit 1
  fi

  if port_is_listening "$FRONTEND_PORT"; then
    echo "Vue 端口 $FRONTEND_PORT 已被占用，跳过前端启动。"
    return 0
  fi

  start_background \
    "Vue 前端" \
    "$VUE_DIR" \
    "$VUE_DIR/.env" \
    "$LOG_DIR/frontend.log" \
    "$PID_DIR/frontend.pid" \
    "$NPM_BIN" run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
}

start_actuator() {
  ensure_executable "$ACTUATOR_PYTHON" "执行器 Python"
  start_background \
    "UI 执行器" \
    "$ACTUATOR_DIR" \
    "" \
    "$LOG_DIR/actuator.log" \
    "$PID_DIR/actuator.pid" \
    "$ACTUATOR_PYTHON" main.py --config config.toml --skip-browser-check

  echo "等待 UI 执行器稳定注册到后端..."
  sleep 5
  if ! pid_matches_service "$(cat "$PID_DIR/actuator.pid" 2>/dev/null || true)" "$ACTUATOR_DIR"; then
    echo "UI 执行器启动后已退出，请查看: $LOG_DIR/actuator.log" >&2
    tail -n 80 "$LOG_DIR/actuator.log" >&2 || true
    exit 1
  fi

  if ! wait_for_actuator 20; then
    echo "UI 执行器未注册到后端，启动任务会返回 503。请查看: $LOG_DIR/actuator.log" >&2
    tail -n 80 "$LOG_DIR/actuator.log" >&2 || true
    exit 1
  fi
}

main() {
  case "${1:-}" in
    -h|--help)
      usage
      exit 0
      ;;
  esac

  mkdir -p "$LOG_DIR" "$PID_DIR"
  cd "$ROOT_DIR"

  if [ "$START_INFRA" = "1" ]; then
    start_infra
  fi

  if [ "$RUN_MIGRATIONS" = "1" ]; then
    run_migrations
  fi

  if [ "$START_BACKEND" = "1" ]; then
    start_backend
  fi

  if [ "$START_CELERY" = "1" ]; then
    start_celery
  fi

  if [ "$START_MCP" = "1" ]; then
    start_mcp
  fi

  if [ "$START_FRONTEND" = "1" ]; then
    start_frontend
  fi

  if [ "$START_ACTUATOR" = "1" ]; then
    start_actuator
  fi

  echo "本地开发环境启动完成。"
  echo "前端: http://127.0.0.1:$FRONTEND_PORT"
  echo "后端: http://127.0.0.1:$BACKEND_PORT"
  echo "MCP:  http://127.0.0.1:8006 / http://127.0.0.1:8007"
  echo "Playwright MCP: http://127.0.0.1:8916"
  echo "日志目录: $LOG_DIR"
  echo "PID 目录: $PID_DIR"
}

main "$@"
