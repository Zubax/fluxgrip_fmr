# Force Measurement Rig (FMR)

This directory contains the sources of the holding force measurement rig (FMR) for FluxGrip, consisting of the following:
- `3d-models`: STL files for the 3d-printed pieces, `.shapr` project file of the entire structure
- `firmware_force_sensor`: Arduino firmware for reading the 2 force sensors
- `firmware_stepper_drive`: Arduino firmware for controlling the movement of the arm up/down
- `force_rig_client`: Client software (Python) to control the rig
  - `force_sensor_client.py`: client for reading out the force sensor values, calibration.
  - `step_drive_control.py`: client for moving the arm up/down (using the stepper drive mounted on top)
  - `force_rig_client.py`: 
    - `--measure`: for measuring the remaining magnetic force (after demagnetization)
    - `--optimize`: will execute a number of sequantial measurements, trying to find the best demag values possible (relies on LLM for optimizing these values)

the firmware for the device, the client PC software, and the CAD models.

## Hardware Setup

## Software Setup

1. Install Python 3.12. The current development environment uses Python 3.12.3.
2. Create and populate the virtual environment from the repository root:

```shell
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

3. On each new shell, activate the environment before running force-rig scripts:

```shell
source .venv/bin/activate
```

4. Flash the firmware for the force sensors and stepper drive (both require an Arduino).
5. Before using the force rig, make sure every component of the system works correctly:
  - serial links
  - force sensors
  - step driver
