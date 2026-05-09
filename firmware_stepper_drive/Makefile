# Copyright (C) 2023 Zubax Robotics

SRC = src/main.c src/platform.c

MCU = atmega328p
DEF = -DF_CPU=16000000

FLAGS  = -O2 -mmcu=$(MCU) -Wl,-u,vfprintf -lprintf_flt -Wl,-u,vfscanf -lscanf_flt -lm
CFLAGS = $(FLAGS) -ffunction-sections -fdata-sections -Wall -Wextra -Werror -pedantic -Wno-unused-parameter \
    -std=c11 -Wno-array-bounds

LDFLAGS = $(FLAGS) -Wl,--gc-sections -Wl,-lgcc -Wl,-lc

DEF += -DNDEBUG

# ---------------

AVRDUDE_PORT ?= /dev/ttyUSB0

# ---------------

COBJ = $(SRC:.c=.o)
SOBJ = $(ASMFILES:.s=.o)
OBJ  = $(COBJ) $(SOBJ)

CROSS_COMPILE = avr-
CC   = $(CROSS_COMPILE)gcc
AS   = $(CROSS_COMPILE)as
LD   = $(CROSS_COMPILE)gcc
CP   = ${CROSS_COMPILE}objcopy
SIZE = ${CROSS_COMPILE}size

# ---------------

all: output.elf output.bin size
	@echo done

%.elf: $(OBJ)
	$(LD) $(LDFLAGS) $(OBJ) -o $@

%.bin: %.elf
	$(CP) -O binary -R .eeprom $< $@

$(COBJ): %.o: %.c
	$(CC) -c $(DEF) $(INC) $(CFLAGS) $< -o $@

clean:
	rm -f output.elf output.bin $(OBJ) a.out

size:
	@echo $(MAKEFILE_LIST)
	@if [ -f output.elf ]; then echo; $(SIZE) $(SOBJ) $(COBJ) -t; echo; fi;

sizex:
	$(SIZE) $(SOBJ) $(COBJ) -t
	@echo
	$(SIZE) output.elf -A

dude: output.bin
	avrdude -p$(MCU) -carduino -P$(AVRDUDE_PORT) -b 57600 -Uflash:w:output.bin

format:
	clang-format -i src/*.[ch]

test:
	gcc $(TEST_FLAGS) test.c -o test -O0 -ggdb -std=c11 -Wall -Wextra -Werror -pedantic -Isrc \
	    -Wno-unused-parameter -Wno-unused-variable -Wno-unused-function \
	    -Wno-unused-but-set-variable -Wno-unused-but-set-parameter -Wno-unused-value -Wno-unused-result \
	    -Wno-unused-label -Wno-unused-local-typedefs -Wno-unused-const-variable -Wno-unused-macros

execute_test: test
	./test

.PHONY: all clean size sizex dude format test execute_test
