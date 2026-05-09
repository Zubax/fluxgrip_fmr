// Copyright (C) 2023 Zubax Robotics

#include "platform.h"
#include <avr/io.h>
#include <util/delay.h>
#include <avr/interrupt.h>

static const uint16_t DRIVER_TIMER_OCR1A_SLOW = 65535U;
static const uint16_t DRIVER_TIMER_OCR1A_FAST = 13106U;  // 5x faster than slow: (65535 + 1) / 5 - 1.

struct pin_spec
{
    volatile uint8_t* const reg;  // The PORT or PIN register for the pin.
    const uint8_t           bit;  // The index of the pin in the register.
};

static inline void pin_write(const struct pin_spec pin, const bool value)
{
    const uint8_t sreg = SREG;
    __asm__("cli");
    if (value)
    {
        *pin.reg |= (1U << pin.bit);
    }
    else
    {
        *pin.reg &= ~(1U << pin.bit);
    }
    SREG = sreg;
}

static inline bool pin_read(const struct pin_spec pin)
{
    return (*pin.reg & (1U << pin.bit)) != 0;
}

void platform_init(void)
{
    __asm__("cli");
    __asm__("wdr");
    WDTCSR |= (1U << WDE) | (1U << WDCE);
    WDTCSR = (1U << WDE) | (1U << WDP3) | (0U << WDP2) | (0U << WDP1) | (1U << WDP0);  // Watchdog, 8 sec

    CLKPR = 0x80;  // Disable prescaler
    CLKPR = 0x00;

    // GPIO
    DDRB  = 1U << 5U;                 // LED on PB5
    PORTB = 0xFFU;                    // All pull-ups, LED on
    DDRD  = (1U << 1U) | (1U << 2U);  // TXD, Load cell SCK
    PORTD = 0xFFU;                    // All pull-ups, SCK high (idle state).

    // Serial port at 38400 baud with 0.2% error. This is the fastest available standard baud rate.
    // For calculation, see http://wormfood.net/avrbaudcalc.php.
    UCSR0A = 0;
    UCSR0B = (1U << 7U) | (1U << 6U) | (1U << 4U) | (1U << 3U);
    UCSR0C = (1U << 2U) | (1U << 1U);
    UBRR0  = 25;  // NOLINT(readability-magic-numbers)

    __asm__("sei");
}

void platform_kick_watchdog(void)
{
    __asm__("wdr");
}

void platform_led(const bool on)
{
    pin_write((struct pin_spec){&PORTB, 5}, on);
}

void platform_driver_setup(void)
{
    // Enable PWM on D9 (PB1) [PULSE]
    // Set Timer1 to CTC mode with Toggle on Compare Match on OC1A
    TCCR1A = (1 << COM1A0); // Toggle OC1A on compare match
    TCCR1B = (1 << WGM12) | (1 << CS10); // CTC mode, prescaler = 1
    OCR1A = DRIVER_TIMER_OCR1A_SLOW; // Frequency value Square wave output

    DDRB |= (1 << PB2);  // Enable output on D10 (PB2) [DIRECTION]
    pin_write((struct pin_spec){&PORTB, 2}, false);
}

void platform_driver_step(bool direction, bool fast)
{
    DDRB |= (1 << PB1);  // Enable output on D9 (PB1) [PULSE]
    OCR1A = fast ? DRIVER_TIMER_OCR1A_FAST : DRIVER_TIMER_OCR1A_SLOW;
    // Update DIR pin
    if (direction)
    {
        pin_write((struct pin_spec){&PORTB, 2}, true);
    }
    else
    {
        pin_write((struct pin_spec){&PORTB, 2}, false);
    }
}

void platform_driver_stop(void)
{
    DDRB &= ~(1 << PB1); // Disable output on D9 (PB1)
}

static uint8_t g_buf_tx[200];
static uint8_t g_buf_rx[500];

struct fifo
{
    uint8_t* const pbuf;
    const size_t   bufsize;
    size_t         in;
    size_t         out;
    size_t         len;
};

static struct fifo g_fifo_tx = {g_buf_tx, sizeof(g_buf_tx), 0, 0, 0};
static struct fifo g_fifo_rx = {g_buf_rx, sizeof(g_buf_rx), 0, 0, 0};

static void fifo_push(struct fifo* const pfifo, const uint8_t data)
{
    const uint8_t sreg = SREG;
    __asm__("cli");
    pfifo->pbuf[pfifo->in++] = data;
    if (pfifo->in >= pfifo->bufsize)
    {
        pfifo->in = 0;
    }
    if (pfifo->len >= pfifo->bufsize)
    {
        pfifo->out++;
        if (pfifo->out >= pfifo->bufsize)
        {
            pfifo->out = 0;
        }
    }
    else
    {
        pfifo->len++;
    }
    SREG = sreg;
}

static int16_t fifo_pop(struct fifo* const pfifo)
{
    int16_t       retval = -1;
    const uint8_t sreg   = SREG;
    __asm__("cli");
    if (pfifo->len <= 0)
    {
        goto _exit;
    }
    pfifo->len--;
    retval = pfifo->pbuf[pfifo->out++];
    if (pfifo->out >= pfifo->bufsize)
    {
        pfifo->out = 0;
    }
    _exit:
        SREG = sreg;
    return retval;
}

static size_t fifo_len(const struct fifo* const pfifo)
{
    const uint8_t sreg = SREG;
    __asm__("cli");
    const size_t retval = pfifo->len;
    SREG                = sreg;
    return retval;
}

static bool is_tx_idle(void)
{
    return (fifo_len(&g_fifo_tx) == 0) && (UCSR0A & (1U << 5U));
}

ISR(USART_TX_vect)
{
    const int16_t val = fifo_pop(&g_fifo_tx);
    if (val >= 0)
    {
        UDR0 = val;
    }
}

ISR(USART_RX_vect)
{
    const uint8_t val = UDR0;
    if ((UCSR0A & ((1U << 4U /*frame error*/) | (1U << 2U /*parity error*/))) == 0)
    {
        fifo_push(&g_fifo_rx, val);
    }
}

void platform_serial_write(const size_t size, const void* const data)
{
    const uint8_t* bytes     = data;
    size_t         remaining = size;
    const uint8_t  sreg      = SREG;
    __asm__("cli");
    if (is_tx_idle())
    {
        UDR0 = *bytes++;
        remaining--;
    }
    SREG = sreg;  // End of the critical section here
    while (remaining-- > 0)
    {
        while (fifo_len(&g_fifo_tx) >= g_fifo_tx.bufsize)
        {
            __asm__ volatile("nop");
        }
        fifo_push(&g_fifo_tx, *bytes++);
    }
}

int16_t platform_serial_read(void)
{
    return fifo_pop(&g_fifo_rx);  // Critical section is not needed here.
}
