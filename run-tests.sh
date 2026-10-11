#!/usr/bin/env bash
#
# Run every suite and report coverage for each.
#
#   ./run-tests.sh              tests only
#   ./run-tests.sh --coverage   tests plus coverage for all three languages
#   ./run-tests.sh --strict     also fail if a suite is skipped (CI uses this)
#
# Coverage tooling, if you want the --coverage numbers:
#   cargo install cargo-llvm-cov
#   uv venv .venv && uv pip install --python .venv/bin/python pytest coverage
# All three are optional: each suite below skips itself if its tools are absent.
#   gem install bashcov
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
COV=0
STRICT=0
for arg in "$@"; do
  case "$arg" in
    --coverage) COV=1 ;;
    --strict) STRICT=1 ;;
    *) echo "usage: $0 [--coverage] [--strict]" >&2; exit 2 ;;
  esac
done
FAILED=0
SKIPPED=()

hdr() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
note() { printf '    %s\n' "$1"; }

# ---------------------------------------------------------------- rust

hdr "rust"
if command -v cargo >/dev/null; then
  if [ "$COV" = 1 ] && cargo llvm-cov --version >/dev/null 2>&1; then
    cargo llvm-cov --manifest-path rust/Cargo.toml --all-targets --summary-only \
      2>&1 | tail -9 || FAILED=1
  else
    # cargo prints one `test result` line per target, and most of them are
    # empty binaries. Sum them instead of showing the first few zeroes.
    out=$(cargo test --manifest-path rust/Cargo.toml 2>&1) || FAILED=1
    grep -E '^test result' <<<"$out" \
      | awk '{p+=$4; f+=$6} END {printf "    %d passed, %d failed\n", p, f}'
  fi
else
  note "cargo not installed; skipping (the Python fallbacks cover the same behaviour)"
  SKIPPED+=(rust)
fi

# ---------------------------------------------------------------- python

hdr "python"
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
if "$PY" -c 'import pytest' 2>/dev/null; then
  if [ "$COV" = 1 ] && "$PY" -c 'import coverage' 2>/dev/null; then
    "$PY" -m coverage erase
    COVERAGE_PROCESS_START="$ROOT/pyproject.toml" PYTHONPATH="$ROOT/tests/_cov" \
      "$PY" -m coverage run --parallel-mode -m pytest tests/ -q 2>&1 | tail -1
    [ "${PIPESTATUS[0]}" = 0 ] || FAILED=1
    "$PY" -m coverage combine >/dev/null 2>&1
    "$PY" -m coverage report -m 2>&1 | tail -8
  else
    "$PY" -m pytest tests/ -q 2>&1 | tail -1
    [ "${PIPESTATUS[0]}" = 0 ] || FAILED=1
  fi
else
  note "pytest not installed; skipping"
  SKIPPED+=(python)
fi

# ---------------------------------------------------------------- shell

hdr "shell"
if [ "$COV" = 1 ] && command -v bashcov >/dev/null; then
  rm -rf coverage
  bashcov --bash-path "$(command -v bash)" --root "$ROOT" -- ./tests/test_apkdiff_sh.sh \
    2>&1 | tail -2
  [ "${PIPESTATUS[0]}" = 0 ] || FAILED=1
  "$PY" - <<'EOF' 2>/dev/null
import json, os
try:
    d = json.load(open('coverage/coverage.json'))
except OSError:
    raise SystemExit
def walk(node):
    for k, v in node.items():
        if isinstance(v, dict) and isinstance(v.get('lines'), list):
            yield k, v['lines']
        elif isinstance(v, dict):
            yield from walk(v)
for f, lines in walk(d):
    if os.path.basename(f) == 'run-tests.sh':
        continue          # the runner itself, not a thing under test
    rel = [x for x in lines if x is not None]
    hit = [x for x in rel if x > 0]
    print(f"    {os.path.basename(f):26} {len(hit):4}/{len(rel):4} = "
          f"{100*len(hit)/max(len(rel),1):5.1f}%")
EOF
else
  bash tests/test_apkdiff_sh.sh 2>&1 | tail -1
  [ "${PIPESTATUS[0]}" = 0 ] || FAILED=1
fi

# ---------------------------------------------------------------- lints

hdr "lints"
if bash -n apkdiff.sh; then
  note "apkdiff.sh parses"
else
  FAILED=1
fi
# A $var immediately followed by a non-ASCII byte (an ellipsis, say) is read by
# bash as part of the variable NAME, which under `set -u` kills the script at
# runtime with an unbound-variable error. This has bitten twice; keep it fatal.
if grep -nE '\$[A-Za-z_][A-Za-z0-9_]*[^ -~]' apkdiff.sh; then
  # shellcheck disable=SC2016  # the text is literal
  printf '    \033[31munbraced $var before a non-ASCII byte (use ${var})\033[0m\n'
  FAILED=1
else
  note "no unbraced variables before non-ASCII characters"
fi

printf '\n'
if [ "$FAILED" = 0 ] && [ "${#SKIPPED[@]}" = 0 ]; then
  printf '\033[32mall suites passed\033[0m\n'
elif [ "$FAILED" = 0 ]; then
  printf '\033[33mpassed, but skipped: %s\033[0m\n' "${SKIPPED[*]}"
  [ "$STRICT" = 1 ] && FAILED=1
else
  printf '\033[31msome suites failed\033[0m\n'
fi
exit "$FAILED"
