#!/usr/bin/env bash
set -euo pipefail

readonly TARGET_DISK="/dev/sda"
readonly EXPECTED_MODEL="ST2000VX017-3CV102"
readonly EXPECTED_SERIAL="WWD534T4"
readonly DATA_LABEL="rlwrld-data"
readonly DATA_MOUNT="/data"
readonly APP_DATA="/data/rlwrld-worklog"
readonly APP_USER="hk"
readonly APP_GROUP="hk"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "Run this script with sudo."
[[ -b ${TARGET_DISK} ]] || fail "${TARGET_DISK} is not a block device."

actual_model=$(lsblk -dn -o MODEL "${TARGET_DISK}" | xargs)
actual_serial=$(lsblk -dn -o SERIAL "${TARGET_DISK}" | xargs)
[[ ${actual_model} == "${EXPECTED_MODEL}" ]] || \
  fail "Model mismatch: expected ${EXPECTED_MODEL}, got ${actual_model}"
[[ ${actual_serial} == "${EXPECTED_SERIAL}" ]] || \
  fail "Serial mismatch: expected ${EXPECTED_SERIAL}, got ${actual_serial}"

mapfile -t block_types < <(lsblk -nr -o TYPE "${TARGET_DISK}")
[[ ${#block_types[@]} -eq 1 && ${block_types[0]} == "disk" ]] || \
  fail "${TARGET_DISK} is no longer empty; refusing to repartition it."

if findmnt -rn -S "${TARGET_DISK}" >/dev/null; then
  fail "${TARGET_DISK} is mounted; refusing to continue."
fi

echo "Preparing ${TARGET_DISK} (${actual_model}, serial ${actual_serial})"
parted --script "${TARGET_DISK}" mklabel gpt mkpart primary ext4 0% 100%
partprobe "${TARGET_DISK}"
udevadm settle

partition="${TARGET_DISK}1"
for _ in {1..10}; do
  [[ -b ${partition} ]] && break
  sleep 1
done
[[ -b ${partition} ]] || fail "Partition ${partition} did not appear."

mkfs.ext4 -F -L "${DATA_LABEL}" "${partition}"
mkdir -p "${DATA_MOUNT}"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -o root -g root -m 0644 \
  "${script_dir}/../deploy/data.mount" \
  /etc/systemd/system/data.mount
systemctl daemon-reload
systemctl enable --now data.mount

install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${APP_DATA}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 \
  "${APP_DATA}/raw" \
  "${APP_DATA}/raw/slack" \
  "${APP_DATA}/raw/google-calendar" \
  "${APP_DATA}/raw/github" \
  "${APP_DATA}/manifests" \
  "${APP_DATA}/exports" \
  "${APP_DATA}/logs"

findmnt "${DATA_MOUNT}"
lsblk -o NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS "${TARGET_DISK}"

echo "Data disk is ready at ${APP_DATA}."
