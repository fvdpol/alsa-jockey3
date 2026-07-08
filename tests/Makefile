# Makefile user-space testing code

CFLAGS += -Wall -O2


test: test_codec
	@echo running test
	./test_codec

test_codec: test_ploytec_codec.o ploytec_codec.o
	$(CC) $(CFLAGS) -isystem stubs -o $@ $^

clean:
	rm *.o test_codec


# create a symlink to the ploytec_codec.c to avoid it pulling-in the kernel headers when compiling the test code
ploytec_codec.c:
	ln -s ../ploytec_codec.c ploytec_codec.c
	ln -s ../ploytec_codec.h ploytec_codec.h

ploytec_codec.o: ploytec_codec.c
	$(CC) $(CFLAGS) -isystem stubs -c $< -o $@


test_ploytec_codec.o: test_ploytec_codec.c
	$(CC) $(CFLAGS) -isystem stubs -c $< -o $@


.PHONY: test_codec
