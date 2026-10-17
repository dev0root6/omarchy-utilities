#!/bin/bash
# Read-only probe of the embedded controller on the Acer Aspire Lite AL15-41.
# Loads ec_sys WITHOUT write support and snapshots the EC registers while idle,
# under CPU load, and after toggling Fn+1, so the fan registers can be verified
# before anything is ever written.
set -euo pipefail

out="$HOME/fan-control/probe"
mkdir -p "$out"
ec=/sys/kernel/debug/ec/ec0/io

sudo modprobe -r ec_sys 2>/dev/null || true
sudo modprobe ec_sys write_support=0

snap() {
  # 16 bytes per row, offset in hex (od ships with coreutils, xxd may be absent)
  sudo od -A x -t x1 -v -w16 "$ec" > "$out/$1.hex"
  echo "  saved $1  (CPU $(sensors k10temp-* 2>/dev/null | awk '/Tctl/ {print $2}'))"
}

echo "Reading EC registers (read-only)..."
grep -m1 -o '1\.[0-9.]*RAC[0-9]*' /sys/class/dmi/id/bios_version > "$out/bios.txt" || cat /sys/class/dmi/id/bios_version > "$out/bios.txt"
sudo gzip -c /sys/firmware/acpi/tables/DSDT > "$out/DSDT.gz"

snap idle-1
sleep 3
snap idle-2

echo "Loading all CPU cores for 25s to spin the fan up..."
pids=()
for _ in $(seq "$(nproc)"); do yes > /dev/null & pids+=($!); done
sleep 25
snap load
kill "${pids[@]}" 2>/dev/null || true

echo
read -rp "Now press Fn+1 once (fan mode toggle), wait for the fan to change, then press Enter... "
snap fn1-on
read -rp "Press Fn+1 again to toggle back, wait a few seconds, then press Enter... "
snap fn1-off

sudo modprobe -r ec_sys
sudo chown -R "$USER" "$out"
echo
echo "Done. Snapshots in $out — tell Claude it finished."
