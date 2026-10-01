CC      ?= cc
CFLAGS  ?= -O3 -Wall -Wextra -std=c11
# -march=native helps but isn't portable across build/run hosts; enable with
#   make NATIVE=1
ifeq ($(NATIVE),1)
CFLAGS += -march=native
endif

omen-enum: omen_enum.c
	$(CC) $(CFLAGS) -o omen-enum omen_enum.c

omen-rank: omen_rank.c
	$(CC) $(CFLAGS) -o omen-rank omen_rank.c

all: omen-enum omen-rank

.PHONY: all clean
clean:
	rm -f omen-enum omen-rank
