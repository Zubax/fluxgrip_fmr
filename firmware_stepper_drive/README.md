# Microstep Driver Controller

This directory contains the firmware that runs on an ATmega328P connected to control a microstep driver (DM556).
To control the motor you will need to send commands using the serial interface:
- `forward`: drive forward
- `stop`: stop
- `back`: drive backwards

The serial port is configured at **38400-8N1**.

## Hardware configuration

| Microstep driver | Arduino      |
|------------------|--------------|
| PUL+             | VDD          |
| PUL-             | D9/PB1 (PWM) |
| DIR+             | VDD          |
| DIR-             | D10/PB2 (GPIO)   |
| ENA+             | **GND**          |
| ENA-             | **VDD**          |

Note: The labeling on the microstep driver for ENA+/ENA- seems to be switched around.

## Development

Use `make` to build, `make dude` to upload to the board (using the built-in Arduino bootloader),
`make format` to invoke Clang-Tidy for autoformatting.
