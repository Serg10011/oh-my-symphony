#!/usr/bin/env bash
# Runs only on a disposable standard GitHub Linux runner for the public fork.
set -euo pipefail
cd "${RUNNER_TEMP:?}"
mode=${1:?expected probe, base or trial}
image=symphony-check:local
container=(docker run --rm --init --name symphony-check --read-only --user 10001:10001
           --network none --cap-drop ALL --security-opt no-new-privileges:true
           --pids-limit 1536 --cpus 4 --memory 8g --memory-swap 8g --ulimit nofile=16384:16384
           --tmpfs /tmp:rw,exec,nosuid,nodev,size=5g,mode=1777
           --mount "type=bind,src=$RUNNER_TEMP/sources.git,dst=/sources.git,readonly")

case "$mode" in
  probe)
    gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
    marker=$(mktemp /tmp/symphony-host-only.XXXXXXXX)
    port_file="$RUNNER_TEMP/probe-port"
    python3 ours/ci/linux/probe.py serve "$gateway" "$port_file" &
    server_pid=$!
    trap 'kill "$server_pid" 2>/dev/null || true; rm -f "$marker"' EXIT
    for _ in {1..50}; do
      [[ -s "$port_file" ]] && break
      sleep 0.1
    done
    port=$(cat "$port_file")
    # Positive control: the same endpoint is reachable from the normal bridge.
    docker run --rm --read-only --network bridge "$image" \
      python -I /opt/check/probe.py control "$gateway" "$port"
    "${container[@]}" "$image" python -I /opt/check/probe.py isolate "$gateway" "$port" "$marker"
    ;;
  base|trial)
    sha=$(git -C sources.git rev-parse --verify "refs/heads/$mode^{commit}")
    echo "Checking $mode at $sha"
    # Project output must never be interpreted as a runner workflow command.
    stop_token=$(cat /proc/sys/kernel/random/uuid)
    echo "::stop-commands::$stop_token"
    trap 'echo "::$stop_token::"' EXIT
    "${container[@]}" "$image" bash /opt/check/suite.sh "$mode" "$sha"
    ;;
  *)
    echo "Unknown check mode" >&2
    exit 2
    ;;
esac
