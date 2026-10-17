#!/bin/bash
# Reinstall the AL15-41 nbfc config after edits and restart the service.
set -euo pipefail
cfg="Acer Aspire Lite AL15-41 custom"
sudo install -Dm644 "$HOME/fan-control/$cfg.json" "/var/lib/nbfc/configs/$cfg.json"
sudo nbfc restart
sleep 3
nbfc status -f 0
echo "Done. Tell Claude."
