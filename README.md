# omarchy-utilities

Small add-ons for an Omarchy (Arch + Hyprland) laptop, an Acer Aspire Lite AL15-41.

| Key | What |
|---|---|
| `Ctrl+L` | ASCII gallery screensaver |
| `Super+Alt+=` / `Super+Alt+-` | Fan speed up / down by 10% |
| `Super+Alt+M` | Fan mode menu (Auto, Silent, Balanced, Performance, Max) |
| `Super+Alt+I` | System dashboard |

## ascii-gallery

`gallery.py` is a generative ASCII art screensaver (numpy). It shows 14 scenes in random order: railway, coding, ocean, space, night sky, night city, piano, plus paintings after Van Gogh, Hokusai, Kandinsky and others. Scenes change about every 30s through one of 9 transitions. Any key or mouse movement exits.

Flags: `--dump WxH` prints one frame, `--bench WxH` times every scene, `--preview WxH DIR` renders PNG previews.

## fan-control

Fan control through [nbfc-linux](https://github.com/nbfc-linux/nbfc-linux) using EC registers checked against the AL15-41 DSDT (BIOS 1.07.22RAC1):

- `0xCE` DUT1: current fan duty, read.
- `0xE7` XFAN: fan override, written. `0` hands control back to the EC's own curve. Measured duty settles at XFAN + 79, so XFAN 176 gives full speed.

Files:

- `Acer Aspire Lite AL15-41 custom.json`: the nbfc model config. Auto mode follows the EC below 60°C, then ramps 30% to 100% between 60°C and 88°C, with forced full speed above 90°C.
- `probe.sh`: a read-only EC snapshot (idle, load, Fn+1), used to verify the registers before writing anything.
- `setup.sh`: installs nbfc-linux and the config, starts it read-only, asks before enabling writes, then calibrates.
- `apply-config.sh`: reinstalls the config after edits and restarts the service.
- `fan-speed`: `up`, `down`, `auto`, `max`, `set N`, `menu`, `status`, with notifications.

## nothing-dash

A Nothing-styled btop alternative served at `http://127.0.0.1:47800`. It shows CPU, memory, fan speed (read-only), temperatures, cores, battery, network and disk, plus a processes tab (two-click END for your own processes) and a boot tab (enabled system and user units with boot time and memory, plus Hyprland autostart). `server.py` uses only the Python standard library.

- `nothing-dash`: the launcher. It starts the service if needed and opens the page as a web app.
- `../systemd/nothing-dash.service`: the user unit, which starts at login.

## Install

The tools are expected under `~/omarchy-utilities`, with these links:

```sh
ln -s ~/omarchy-utilities/ascii-gallery ~/ascii-gallery
ln -s ~/omarchy-utilities/fan-control ~/fan-control
ln -s ~/omarchy-utilities/nothing-dash ~/nothing-dash
ln -s ~/omarchy-utilities/fan-control/fan-speed ~/.local/bin/fan-speed
ln -s ~/omarchy-utilities/nothing-dash/nothing-dash ~/.local/bin/nothing-dash
cp ~/omarchy-utilities/systemd/nothing-dash.service ~/.config/systemd/user/
systemctl --user enable --now nothing-dash
cat ~/omarchy-utilities/hypr/bindings.conf >> ~/.config/hypr/bindings.conf
~/fan-control/setup.sh   # needs sudo, asks before any EC write
```

`hypr/bindings.conf` holds the binds exactly as they are in the live config. Note that the global `Ctrl+L` bind swallows Ctrl+L in every app.
