#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
export UV_CACHE_DIR

django_python() {
  if [[ -x "$ROOT_DIR/WHartTest_Django/.venv/bin/python" ]]; then
    "$ROOT_DIR/WHartTest_Django/.venv/bin/python" "$@"
  else
    uv run python "$@"
  fi
}

echo "[1/4] Django system check"
(
  cd "$ROOT_DIR/WHartTest_Django"
  django_python manage.py check
)

echo "[2/4] Django AI generation compile check"
(
  cd "$ROOT_DIR/WHartTest_Django"
  django_python -m py_compile \
    ui_automation/ai_planning.py \
    ui_automation/views.py \
    ui_automation/consumers.py \
    ui_automation/element_map_governance.py
)

echo "[3/4] Actuator compile check"
(
  cd "$ROOT_DIR/WHartTest_Actuator"
  ./.venv/bin/python -m py_compile \
    consumer.py \
    executor.py \
    main.py \
    models.py
)

echo "[4/4] Vue type check"
(
  cd "$ROOT_DIR/WHartTest_Vue"
  ./node_modules/.bin/vue-tsc -b
)

echo "AI UI generation checks passed"
