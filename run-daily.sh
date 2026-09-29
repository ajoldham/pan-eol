#!/usr/bin/env bash
#
# Run pan-eol.py once, wait 24 hours, run it again, and repeat until Ctrl-C.
#
# Usage:
#   ./run-daily.sh                          # default options
#   ./run-daily.sh --format both -q         # any options are passed to pan-eol.py
#   INTERVAL_SECONDS=3600 ./run-daily.sh    # change the wait (default 86400 = 24h)
#
# The countdown is shown on a single line, and only when the output is a
# terminal. When redirected to a log file, the script just waits quietly.
#

INTERVAL_SECONDS="${INTERVAL_SECONDS:-86400}"

# Run from the project folder so output/ and state/ always land in the same place.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi

case "$INTERVAL_SECONDS" in
    '' | *[!0-9]*)
        echo "INTERVAL_SECONDS must be a whole number of seconds, got '$INTERVAL_SECONDS'" >&2
        exit 2
        ;;
esac

IS_TTY=0
[ -t 1 ] && IS_TTY=1

cleanup() {
    # Show the cursor again, which the countdown hides.
    [ "$IS_TTY" -eq 1 ] && printf '\033[?25h'
}

on_interrupt() {
    # Clear the countdown line (if any) and exit with the usual Ctrl-C status.
    [ "$IS_TTY" -eq 1 ] && printf '\r\033[K'
    echo "Stopped."
    exit 130
}

trap cleanup EXIT
trap on_interrupt INT TERM

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

# Format an epoch time as local date/time (BSD date on macOS, GNU date on Linux).
format_epoch() {
    date -r "$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$1" '+%Y-%m-%d %H:%M:%S'
}

run_once() {
    echo "[$(timestamp)] Running pan-eol.py $*"
    "$PYTHON" pan-eol.py "$@"
    local rc=$?
    case "$rc" in
        0) echo "[$(timestamp)] Finished: no changes (exit 0)" ;;
        3) echo "[$(timestamp)] Finished: CHANGES DETECTED (exit 3)" ;;
        *) echo "[$(timestamp)] Finished with an error (exit $rc); will try again next cycle" ;;
    esac
}

countdown() {
    # Count down to a fixed end time rather than sleeping N times. The timer
    # stays accurate even if the Mac sleeps or the loop runs slowly.
    local target=$(($(date +%s) + INTERVAL_SECONDS))
    local next
    next="$(format_epoch "$target")"

    if [ "$IS_TTY" -eq 0 ]; then
        echo "[$(timestamp)] Next run at $next"
        while [ "$(date +%s)" -lt "$target" ]; do
            sleep 1
        done
        return
    fi

    printf '\033[?25l' # hide cursor while counting down
    local now remaining
    while :; do
        now=$(date +%s)
        remaining=$((target - now))
        [ "$remaining" -le 0 ] && break
        printf '\r\033[KNext run in %02d:%02d:%02d (at %s)  -  press Ctrl-C to quit' \
            $((remaining / 3600)) $((remaining % 3600 / 60)) $((remaining % 60)) "$next"
        sleep 1
    done
    printf '\r\033[K\033[?25h'
}

echo "pan-eol daily runner: every $INTERVAL_SECONDS seconds. Press Ctrl-C to quit."
while :; do
    run_once "$@"
    countdown
done
