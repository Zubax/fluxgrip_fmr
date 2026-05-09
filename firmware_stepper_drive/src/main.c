// Copyright (C) 2023 Zubax Robotics

#include "platform.h"
#include "packet.h"

#include <string.h>

struct drive_command
{
    int32_t step;
    int32_t speed;
};
_Static_assert(sizeof(struct drive_command) == 8, "Invalid layout");

enum
{
    DRIVE_SPEED_SLOW = 0,
    DRIVE_SPEED_FAST = 1,
};

static struct drive_command normalize_command(struct drive_command command)
{
    if ((command.step < -1) || (command.step > 1))
    {
        command.step = 0;
    }
    if (command.speed != DRIVE_SPEED_FAST)
    {
        command.speed = DRIVE_SPEED_SLOW;
    }
    return command;
}

static void execute_command(const struct drive_command command)
{
    switch (command.step) {
    case -1:
        platform_driver_step(false, command.speed == DRIVE_SPEED_FAST);
        break;
    case 1:
        platform_driver_step(true, command.speed == DRIVE_SPEED_FAST);
        break;
    case 0:
    default:
        platform_driver_stop();
    }
}

int main(void)
{
    struct packet_parser parser  = {0};
    struct drive_command received_command = {0};

    platform_init();
    platform_driver_setup();
    execute_command(received_command);

    while (true)
    {
        platform_kick_watchdog();

        // Step in the current direction/speed and echo it for command acknowledgement.
        execute_command(received_command);
        packet_send(sizeof(received_command), &received_command, platform_serial_write);

        // Process the pending incoming data. There may be many bytes accumulated in the buffer.
        while (true)
        {
            const int16_t rx = platform_serial_read();
            if (rx < 0)
            {
                break;
            }
            if (packet_parse(&parser, (uint8_t) rx))
            {
                if (parser.payload_size == sizeof(received_command))
                {
                    memcpy(&received_command, parser.payload, sizeof(received_command));
                    received_command = normalize_command(received_command);
                }
                else if (parser.payload_size == sizeof(int32_t))
                {
                    struct drive_command legacy_command = {0};
                    memcpy(&legacy_command.step, parser.payload, sizeof(legacy_command.step));
                    received_command = normalize_command(legacy_command);
                }
            }
        }
    }
    return 0;
}
