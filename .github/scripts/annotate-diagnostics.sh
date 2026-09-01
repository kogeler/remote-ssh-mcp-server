#!/usr/bin/env bash

# Copy standard input to standard output unchanged and, on GitHub Actions,
# additionally emit one workflow annotation per gcc-style diagnostic line so a
# failure appears on the exact line of the pull-request diff.
#
# Outside GitHub Actions this is a transparent pass-through, which keeps local
# tool output identical to a run without it.
#
# Usage: <tool> | annotate-diagnostics.sh <title>

set -euo pipefail

if (($# != 1)); then
    printf 'Usage: %s <title>\n' "${0##*/}" >&2
    exit 2
fi

title="$1"
with_column='^([^[:space:]:]+):([0-9]+):([0-9]+): (error|warning|note): (.*)$'
without_column='^([^[:space:]:]+):([0-9]+): (error|warning|note): (.*)$'

annotate() {
    local file="$1" line="$2" column="$3" severity="$4" message="$5"
    local level position

    case "${severity}" in
        error) level='error' ;;
        warning) level='warning' ;;
        *) level='notice' ;;
    esac

    position="file=${file},line=${line}"
    if [[ -n "${column}" ]]; then
        position="${position},col=${column}"
    fi

    # A literal percent sign would otherwise start an escape sequence.
    printf '::%s %s,title=%s::%s\n' \
        "${level}" "${position}" "${title}" "${message//%/%25}"
}

while IFS= read -r line || [[ -n "${line}" ]]; do
    printf '%s\n' "${line}"

    if [[ "${GITHUB_ACTIONS:-}" != 'true' ]]; then
        continue
    fi

    if [[ "${line}" =~ ${with_column} ]]; then
        annotate "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" \
            "${BASH_REMATCH[4]}" "${BASH_REMATCH[5]}"
    elif [[ "${line}" =~ ${without_column} ]]; then
        annotate "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" '' \
            "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"
    fi
done
