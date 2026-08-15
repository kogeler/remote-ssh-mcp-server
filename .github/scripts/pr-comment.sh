#!/usr/bin/env bash

# Publish one Markdown report as a single sticky pull-request comment.
#
# Every marker owns exactly one comment: the first run creates it and later runs
# on the same pull request rewrite it in place instead of appending noise.
#
# Usage: pr-comment.sh <marker> <body-file>
# Environment: GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER

set -euo pipefail

if (($# != 2)); then
    printf 'Usage: %s <marker> <body-file>\n' "${0##*/}" >&2
    exit 2
fi

marker_name="$1"
body_file="$2"
marker="<!-- remote-ssh-mcp:${marker_name} -->"

for variable in GH_TOKEN GITHUB_REPOSITORY PR_NUMBER; do
    if [[ -z "${!variable:-}" ]]; then
        printf 'Required environment variable is empty: %s\n' "${variable}" >&2
        exit 1
    fi
done

if [[ ! -r "${body_file}" ]]; then
    printf 'Comment body is missing or unreadable: %s\n' "${body_file}" >&2
    exit 1
fi

body="${marker}"$'\n\n'"$(cat -- "${body_file}")"

# gh embeds its own jq, so no external JSON tooling is required on the runner.
# A command substitution keeps an API failure fatal instead of silently
# creating a duplicate comment.
matching_ids="$(
    gh api --paginate \
        "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
        --jq ".[] | select(.body | startswith(\"${marker}\")) | .id"
)"
existing_id="${matching_ids%%$'\n'*}"

if [[ -n "${existing_id}" ]]; then
    gh api --silent --method PATCH \
        "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_id}" \
        --raw-field "body=${body}"
    printf 'Updated %s comment %s on pull request %s\n' \
        "${marker_name}" "${existing_id}" "${PR_NUMBER}"
else
    gh api --silent --method POST \
        "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
        --raw-field "body=${body}"
    printf 'Created %s comment on pull request %s\n' \
        "${marker_name}" "${PR_NUMBER}"
fi
