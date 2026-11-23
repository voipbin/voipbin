#!/bin/bash
# komodo-hook-format: 1
# VoIPBin Komodo pre_deploy hook (installed by komodo-setup.sh as
# /usr/local/lib/voipbin/komodo-hook.sh, root owned, mode 755).
#
# Komodo runs this as root before every DeployStack of the "voipbin" Stack,
# with the install directory as the working directory. It is a gate against
# deploys that overlap a running voipbin operation, not a permission boundary.
#
#   no .voipbin-op.lock                     -> pass
#   lock holder pid not running (stale)     -> pass
#   live lock + valid one-time token        -> consume token, pass
#   anything else                           -> refuse (exit 7)
#
# Token (.voipbin-hook-token): "<lock pid> <issued epoch seconds>", written by
# the CLI right before it calls DeployStack while it holds the lock. Valid when
# it names the current lock pid and is at most 60 seconds old. It is deleted on
# use, so it cannot be replayed.
#
# This script never runs another script from the repository. It only reads the
# two files above and deletes a used token. Both files must be regular files
# (symlinks are refused) owned by root or by the owner of the install directory.

set -u

LOCK_FILE=".voipbin-op.lock"
TOKEN_FILE=".voipbin-hook-token"
TOKEN_MAX_AGE=60

deny() {
    echo "voipbin operation in progress${1:+ ($1)}" >&2
    exit 7
}

# trusted_file <path>: regular file, not a symlink, owned by root or by the
# owner of the install directory.
trusted_file() {
    local f="$1" uid dir_uid
    [[ -f "$f" && ! -L "$f" ]] || return 1
    uid=$(stat -c %u -- "$f" 2>/dev/null) || return 1
    dir_uid=$(stat -c %u -- . 2>/dev/null) || return 1
    [[ "$uid" == "0" || "$uid" == "$dir_uid" ]]
}

# pid_alive <pid>: kill -0 succeeds, or fails only because of EPERM
# (the process exists but belongs to someone else).
pid_alive() {
    local err
    if err=$(LC_ALL=C kill -0 "$1" 2>&1); then
        return 0
    fi
    [[ "$err" == *"not permitted"* ]]
}

# No lock at all (symlink or odd file types included): a plain absence only.
if [[ ! -e "$LOCK_FILE" && ! -L "$LOCK_FILE" ]]; then
    exit 0
fi

trusted_file "$LOCK_FILE" || deny "lock file not trusted"

lock_pid=""
lock_line=""
IFS= read -r lock_line < "$LOCK_FILE" || [[ -n "$lock_line" ]] || true
if [[ "$lock_line" =~ [[:space:]]pid=([0-9]+)([[:space:]]|$) ]]; then
    lock_pid="${BASH_REMATCH[1]}"
fi
[[ -n "$lock_pid" && "$lock_pid" -gt 0 ]] 2>/dev/null || deny "lock file unreadable"

if ! pid_alive "$lock_pid"; then
    echo "voipbin hook: stale lock (pid $lock_pid is not running), continuing"
    exit 0
fi

trusted_file "$TOKEN_FILE" || deny "no valid token"

token_line=""
IFS= read -r token_line < "$TOKEN_FILE" || [[ -n "$token_line" ]] || true
if [[ ! "$token_line" =~ ^([0-9]{1,9})[[:space:]]+([0-9]{1,12})[[:space:]]*$ ]]; then
    deny "no valid token"
fi
token_pid="${BASH_REMATCH[1]}"
token_time="${BASH_REMATCH[2]}"

[[ "$((10#$token_pid))" == "$lock_pid" ]] || deny "token does not match the lock"

now=$(date +%s)
# 10# keeps a leading zero from being read as octal
age=$((now - 10#$token_time))
if (( age < 0 || age > TOKEN_MAX_AGE )); then
    deny "token expired"
fi

# Consume before passing: a token that cannot be removed must not be usable.
rm -f -- "$TOKEN_FILE" 2>/dev/null
if [[ -e "$TOKEN_FILE" || -L "$TOKEN_FILE" ]]; then
    deny "token not consumable"
fi
exit 0
