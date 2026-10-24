#!/usr/bin/env bash
# Load one run's credentials into the current shell:  source scripts/greenops-env.sh [file]
#
# Reads greenops.env (or $1), then checks what the orchestrator needs before it
# touches the network: all five variables present, and the grid endpoint sharing
# the cloud endpoint's scheme, host and /runs/<id>/ prefix -- CloudEnv.__init__
# exits on a mismatch, which would burn a live run's clock.

_go_file="${1:-${BASH_SOURCE[0]%/*}/../greenops.env}"

if [ -f "$_go_file" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$_go_file"
  set +a
else
  echo "greenops-env: $_go_file not found, using the current environment" >&2
fi

_go_bad=
for _go_var in EXOSCALE_API_KEY EXOSCALE_API_SECRET GREENOPS_CLOUD_ENDPOINT \
               GREENOPS_GRID_ENDPOINT GREENOPS_GRID_SECRET; do
  case "${!_go_var}" in
    ""|"...") _go_bad="$_go_bad $_go_var" ;;
  esac
done

if [ -n "$_go_bad" ]; then
  echo "greenops-env: unset or still a placeholder:$_go_bad" >&2
else
  _go_tail="${GREENOPS_CLOUD_ENDPOINT#*/runs/}"
  _go_run="${_go_tail%%/*}"
  _go_prefix="${GREENOPS_CLOUD_ENDPOINT%%/runs/*}/runs/$_go_run/"
  if [ "$_go_tail" = "$GREENOPS_CLOUD_ENDPOINT" ]; then
    echo "greenops-env: GREENOPS_CLOUD_ENDPOINT has no /runs/<id>/ path" >&2
    _go_bad=endpoint
  elif [ "${GREENOPS_GRID_ENDPOINT#"$_go_prefix"}" = "$GREENOPS_GRID_ENDPOINT" ]; then
    echo "greenops-env: grid endpoint is outside $_go_prefix (two different runs)" >&2
    _go_bad=mismatch
  else
    echo "greenops-env: run $_go_run ready"
  fi
fi

unset _go_file _go_var _go_tail _go_run _go_prefix
if [ -n "$_go_bad" ]; then unset _go_bad; return 1 2>/dev/null || exit 1; fi
unset _go_bad
