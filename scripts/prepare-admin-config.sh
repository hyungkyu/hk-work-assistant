#!/usr/bin/env bash
set -euo pipefail

config_base="${XDG_CONFIG_HOME:-${HOME}/.config}"
config_root="${APP_CONFIG_HOST_ROOT:-${config_base}/hk-work-assistant}"

umask 077
install -d -m 0700 "${config_root}" "${config_root}/credentials"

echo "Admin configuration directory is ready at ${config_root}."
echo "Open http://127.0.0.1:8081/backoffice to create the emergency super-administrator password."
