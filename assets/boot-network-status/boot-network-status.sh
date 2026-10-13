#!/bin/sh
# Var-only overlay for the hash-pinned SSM startup view. Never edits services.
# JIBO_NETSTATUS_ROOT is a local test seam; normal boot leaves it unset.
root=${JIBO_NETSTATUS_ROOT:-}
asset="$root/var/jibo/phoenix/network-status/startup-view.js"
target="$root/usr/local/bin/jibo-ssm/startup/startup-view.js"
log="$root/var/jibo/phoenix/network-status/boot-network-status.log"
result=failed

# Shipped BusyBox has no timeout applet. Native probes keep a two-second
# budget; the Node OTA check gets five seconds for cold starts on slower hosts.
# The watchdog owns the timeout receipt (exit 124). Preserve a completed
# command's status, and translate only our SIGKILL (137), never a guard's 1.
# None of these commands forks workers: getmode is Node, hashes/mount are native.
bounded() {
    "$@" &
    command_pid=$!
    (
        trap 'kill "$sleep_pid" 2>/dev/null; exit 0' TERM
        sleep "${bounded_seconds:-2}" &
        sleep_pid=$!
        wait "$sleep_pid"
        # Once the deadline wins, parent cleanup must not erase its receipt.
        trap '' TERM
        kill -KILL "$command_pid" 2>/dev/null && exit 124
        exit 0
    ) >/dev/null 2>&1 &
    watchdog_pid=$!
    wait "$command_pid" 2>/dev/null
    command_status=$?
    kill "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null
    watchdog_status=$?
    if [ "$command_status" = 137 ] && [ "$watchdog_status" = 124 ]; then
        return 124
    fi
    return "$command_status"
}

apply_status() {
    mode=$(bounded "$root/usr/bin/jibo-getmode") || return
    case "$mode" in
        int-developer|developer) ;;
        *) result=skipped-mode; return ;;
    esac
    [ -r "$root/proc/self/mountinfo" ] || return
    while read -r id parent device mountroot point rest; do
        if [ "$point" = "$target" ]; then
            result=already-mounted
            return
        fi
    done < "$root/proc/self/mountinfo"
    [ -f "$target" ] && [ ! -L "$target" ] || return
    sum=$(bounded "$root/usr/bin/sha256sum" "$target") || return
    case "${sum%% *}" in
        66824555951d3be55d74f7d736655f11e945dea887e52f71ff2a5fa2064c14d6)
            name=startup-view.js
            expected=3f21af2c709030ae07c88638061094b4d6630b4bc77164dda327d29da9877bd6 ;;
        8f2ba0385c5030d5bf2d86214eb28f272866b72d5ea825cd44650b278e0b1d19)
            name=startup-view-x11-early.js
            expected=3aabd7077dbbae494014aa4f6b2276f50c7ab7ac9a8ae822b10779b03c930c6a ;;
        ba8b9c43dd1e1017bface6886ddfe72a9f6e1bbcf2f5710a37460e6082f18848|f7042f1bcdc0d4120ca315633a447460bbd01b9806281544a49ec28321ce2ff7)
            name=startup-view-dom.js
            expected=60e01ecee4e096de247b3617f81b6c5c4381358ddbb278451176c7c9794df26a ;;
        *) result=skipped-stock-hash; return ;;
    esac
    asset="$root/var/jibo/phoenix/network-status/$name"
    [ -f "$asset" ] && [ ! -L "$asset" ] || return
    sum=$(bounded "$root/usr/bin/sha256sum" "$asset") || return
    [ "${sum%% *}" = "$expected" ] || {
        result=skipped-asset-hash
        return
    }
    helper="$root/var/jibo/phoenix/network-status/ota-safe-overlay.js"
    [ -f "$helper" ] && [ ! -L "$helper" ] || return
    sum=$(bounded "$root/usr/bin/sha256sum" "$helper") || return
    [ "${sum%% *}" = "a5e2bd2af5d33ee0e29af13ba84cb4933661787f2169017881e576ae5214f3e8" ] || return
    bounded_seconds=5 bounded "$root/usr/bin/node" "$helper" check "$root" "$name"
    [ "$?" = 1 ] || { result=skipped-ota; return; }
    bounded "$root/bin/mount" -n -o bind "$asset" "$target" || return
    # Release the exact owned child bind when an OTA becomes pending. All I/O
    # stays local. Detach stdio so this monitor cannot hold ifup/boot open.
    "$root/usr/bin/node" "$helper" watch "$root" "$name" </dev/null >/dev/null 2>&1 &
    result=bound
}

apply_status >/dev/null 2>&1
# Constant outcomes only: never log a mode error, SSID, IP, or command output.
# The root-owned feature directory avoids a world-writable /var/log filename.
# Refuse symlinks/FIFOs/devices; logging failure must not corrupt data or hang boot.
if [ ! -L "$log" ] && { [ ! -e "$log" ] || [ -f "$log" ]; }; then
    { printf 'boot-network-status: %s\n' "$result" >> "$log"; } 2>/dev/null || true
fi
exit 0
