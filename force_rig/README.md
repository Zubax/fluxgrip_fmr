# Force Rig Bring-Up

Use this checklist when setting up the force measurement rig. Run commands from the repository root unless noted
otherwise.

## 1. Create Stable Serial Links

The force sensor and step-drive controllers both enumerate as `/dev/ttyUSB*`, and the numbering can change after
replugging. Create stable symlinks first:

```shell
sudo ./force_rig/setup_serial_links.py
```

The script asks you to disconnect/reconnect the step-drive USB adapter and creates:

```text
/dev/fmr_force_sensor
/dev/fmr_step_drive
```

All force-rig clients use these paths by default.

## 2. Calibrate And Check Force Sensors

Calibrate the two force sensors together. Both sensors are attached to the same platform, so each calibration datapoint
samples both channels at once:

```shell
./force_rig/force_sensor_client.py calibrate --samples 100
```

Use at least two datapoints:

- A known force on the platform.
- A zero-force/no-load datapoint.

After calibration, check that live force readings look correct:

```shell
./force_rig/force_sensor_client.py display
```

Useful options:

```shell
./force_rig/force_sensor_client.py display --no-tare
./force_rig/force_sensor_client.py display --tare-samples 100
```

## 3. Check Step-Drive Control

Make sure the arm has clearance before running these commands.

```shell
./force_rig/step_drive_client.py up --duration 0.5 --speed slow
./force_rig/step_drive_client.py down --duration 0.5 --speed slow
./force_rig/step_drive_client.py stop
```

The move commands show a duration progress bar and always send `STOP` when the command finishes or is interrupted.

## 4. Check FluxGrip Control

Make sure `CYPHAL_PATH` points to the DSDL root namespaces before using the FluxGrip client. The client auto-detects the
available `com.zubax.fluxgrip` node by default.

```shell
./force_rig/fluxgrip_client.py magnetize
./force_rig/fluxgrip_client.py demagnetize
```

Set demag values with exactly 64 signed integer values:

```shell
./force_rig/fluxgrip_client.py set-demag --values "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0"
```

Or load values from a file:

```shell
./force_rig/fluxgrip_client.py set-demag --file demag_values.txt
```

To set values and run a magnetize/demagnetize cycle:

```shell
./force_rig/fluxgrip_client.py cycle --file demag_values.txt --settle 1.0
```
