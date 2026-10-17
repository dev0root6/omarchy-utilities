#!/bin/bash
# Install nbfc-linux with the custom AL15-41 config, check it read-only first,
# then (after confirmation) enable fan writes and run a short calibration.
# Everything is logged to ~/fan-control/setup.log.
set -uo pipefail

dir="$HOME/fan-control"
cfg="Acer Aspire Lite AL15-41 custom"
log="$dir/setup.log"
exec > >(tee "$log") 2>&1

step() { printf '\n== %s\n' "$*"; }
duty() { nbfc status -j 2>/dev/null | jq -c '.Fans[0] | {CurrentSpeed, TargetSpeed, RequestedSpeed, AutoMode, Temperature}' 2>/dev/null || nbfc status; }

step "Installing nbfc-linux"
yay -S --needed --noconfirm nbfc-linux || exit 1

step "Installing config"
sudo install -Dm644 "$dir/$cfg.json" "/var/lib/nbfc/configs/$cfg.json"
sudo nbfc config -s "$cfg"
sudo nbfc sensors set -f 0 -s k10temp -a Max

step "Read-only start (no EC writes)"
sudo nbfc restart -r
sleep 4
nbfc status
duty

echo
read -rp "Does the read-only status above look sane (temperature ~40-90, a speed shown)? Enable fan writes? [y/N] " ok
[[ $ok == [yY]* ]] || { sudo nbfc stop; echo "Stopped; nothing was written."; exit 0; }

step "Write mode"
sudo nbfc restart
sleep 3
if nbfc set -a &>/dev/null; then
  echo "nbfc set works without sudo"
else
  step "Allowing passwordless 'nbfc set' for $USER"
  rule=$(mktemp)
  echo "$USER ALL=(root) NOPASSWD: /usr/bin/nbfc set *" >"$rule"
  if sudo visudo -cf "$rule"; then
    sudo install -m440 -o root -g root "$rule" /etc/sudoers.d/nbfc-fan-speed
  fi
  rm -f "$rule"
fi

step "Calibration: each speed held 10s, then back to auto"
for s in 30 60 100; do
  sudo nbfc set -s "$s"
  sleep 10
  printf 'requested %3s%% -> ' "$s"
  duty
done
sudo nbfc set -a
sleep 5
printf 'auto -> '
duty

step "Enabling service at boot"
sudo systemctl enable nbfc_service

echo
echo "Done. Tell Claude it finished (log: $log)."
