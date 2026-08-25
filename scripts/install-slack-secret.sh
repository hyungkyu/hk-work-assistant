#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="${project_dir}/secrets"
secret_file="${secret_dir}/slack.env"

umask 077
mkdir -p "${secret_dir}"
read -r -s -p "Slack User OAuth Token (xoxp-...): " slack_token
printf '\n'
if [[ "${slack_token}" != xoxp-* ]]; then
  echo "Expected a user OAuth token beginning with xoxp-." >&2
  exit 1
fi

read -r -p "Expected Slack team ID [T077WTVBF8W]: " team_id
team_id="${team_id:-T077WTVBF8W}"
temporary_file="$(mktemp "${secret_dir}/.slack.env.XXXXXX")"
trap 'rm -f "${temporary_file}"' EXIT
printf 'SLACK_USER_TOKEN=%s\nSLACK_EXPECTED_TEAM_ID=%s\n' "${slack_token}" "${team_id}" >"${temporary_file}"
chmod 600 "${temporary_file}"
mv "${temporary_file}" "${secret_file}"
trap - EXIT
echo "Saved ${secret_file} with mode 0600."
