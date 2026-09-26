#!/usr/bin/env bash
# Drives scripts/evenhub-sim-guarded.sh with fake SIM_BIN simulators and checks
# its exit contract (XERK-1030):
#   - a malformed env value exits 2 without starting anything
#   - the simulator's own exit status passes through
#   - a group over SIM_RSS_LIMIT_MB is killed and the guard exits 137
#   - SIGTERM exits 143 with nothing left running, even when the simulator
#     ignores TERM and has to be KILLed
#
# A fake xvfb-run on PATH drops its options and execs the command, so no X
# server is needed. Run: scripts/tests/evenhub-sim-guarded.test.sh
# The fake simulators below are bodies for other scripts: they expand there, not here.
# shellcheck disable=SC2016
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
guard=$here/../evenhub-sim-guarded.sh
tmp=$(mktemp -d)
trap 'pkill -KILL -f "$tmp" 2>/dev/null || true; rm -rf "$tmp"' EXIT

# Fake xvfb-run: skip `-a` and `-s <args>`, then exec the simulator.
cat >"$tmp/xvfb-run" <<'SH'
#!/usr/bin/env bash
while [[ $1 == -* ]]; do
  if [[ $1 == -s ]]; then shift; fi
  shift
done
exec "$@"
SH
chmod +x "$tmp/xvfb-run"
export PATH=$tmp:$PATH

# fake_sim NAME BODY: a simulator script at $tmp/NAME. Its path contains $tmp,
# so `pgrep -f "$tmp"` finds anything the guard left running.
fake_sim() {
  printf '#!/usr/bin/env bash\n%s\n' "$2" >"$tmp/$1"
  chmod +x "$tmp/$1"
}

failures=0
check() { # check NAME EXPECTED ACTUAL
  if [[ $2 == "$3" ]]; then
    echo "ok   $1"
  else
    echo "FAIL $1: expected $2, got $3"
    failures=$((failures + 1))
  fi
}
leftovers() { pgrep -f "$tmp/" >/dev/null && echo yes || echo none; }

# 1. Malformed env values fail closed with 2, before the simulator starts.
fake_sim never 'touch "$0.ran"'
for bad in "SIM_RSS_LIMIT_MB=1500M" "SIM_RSS_LIMIT_MB=0" "SIM_POLL_SECONDS=100" \
  "SIM_POLL_SECONDS=-1"; do
  status=0
  env "$bad" SIM_BIN="$tmp/never" "$guard" 2>/dev/null || status=$?
  check "bad env $bad exits 2" 2 "$status"
done
check "bad env never starts the simulator" absent \
  "$([[ -e $tmp/never.ran ]] && echo present || echo absent)"

# 2. The simulator's own exit status passes through, arguments intact.
fake_sim exits7 '[[ $1 == "a b" && $2 == c ]] || exit 99; exit 7'
status=0
SIM_POLL_SECONDS=1 SIM_BIN="$tmp/exits7" "$guard" "a b" c || status=$?
check "simulator exit status passes through" 7 "$status"

# 3. Over the limit: the whole group is KILLed and the guard exits 137.
# The child holds ~64 MiB against a 16 MiB limit; the parent just waits on it.
fake_sim hog '"$0.child" & wait'
fake_sim hog.child 'x=$(head -c 67108864 /dev/zero | tr "\0" a); sleep 60; echo "${#x}"'
status=0
SIM_RSS_LIMIT_MB=16 SIM_POLL_SECONDS=1 SIM_BIN="$tmp/hog" "$guard" 2>/dev/null || status=$?
check "over the RSS limit exits 137" 137 "$status"
check "over the RSS limit leaves nothing running" none "$(leftovers)"

# 4. SIGTERM: exit 143, and a simulator that ignores TERM is KILLed.
fake_sim stubborn 'trap "" TERM; "$0.child" & while :; do sleep 0.2; done'
fake_sim stubborn.child 'trap "" TERM; while :; do sleep 0.2; done'
SIM_POLL_SECONDS=5 SIM_BIN="$tmp/stubborn" "$guard" &
guard_pid=$!
for _ in $(seq 50); do pgrep -f "$tmp/stubborn.child" >/dev/null && break; sleep 0.1; done
start=$SECONDS
kill -TERM "$guard_pid"
status=0
wait "$guard_pid" || status=$?
check "SIGTERM exits 143" 143 "$status"
check "SIGTERM is handled without waiting out the poll" yes \
  "$( ((SECONDS - start < 5)) && echo yes || echo no)"
check "SIGTERM leaves nothing running" none "$(leftovers)"

if ((failures)); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
