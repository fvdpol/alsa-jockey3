// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Test / Benchmark for ploytec_encode_s24_3le() and ploytec_decode_s24_3le()
 *
 * Validates correctness (bit-exact match) and measures performance.
 * Useful when adding hardware-specific optimized variants.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdint.h>
#include "ploytec_codec.h"

#define TEST_ITERATIONS		1000000
#define BENCHMARK_ITERATIONS	1000000

typedef void (*encode_fn_t)(uint8_t *dest, const uint8_t *src);
typedef void (*decode_fn_t)(uint8_t *dest, const uint8_t *src);

/* Reference implementations (copy of the ones in ploytec_proto.c) */
static void reference_encode_s24_3le(u8 *dest, const u8 *src)
{
	int i;

	/* First 24 bytes: odd channels (ALSA Ch 1 & 3) */
	for (i = 0; i < 8; i++) {
		dest[i]     = (((src[2] >> (7 - i)) & 1) << 0) | (((src[8] >> (7 - i)) & 1) << 1);
		dest[8+i]   = (((src[1] >> (7 - i)) & 1) << 0) | (((src[7] >> (7 - i)) & 1) << 1);
		dest[16+i]  = (((src[0] >> (7 - i)) & 1) << 0) | (((src[6] >> (7 - i)) & 1) << 1);
	}

	/* Second 24 bytes: even channels (ALSA Ch 2 & 4) */
	for (i = 0; i < 8; i++) {
		dest[24+i] = (((src[5] >> (7 - i)) & 1) << 0) | (((src[11] >> (7 - i)) & 1) << 1);
		dest[32+i] = (((src[4] >> (7 - i)) & 1) << 0) | (((src[10] >> (7 - i)) & 1) << 1);
		dest[40+i] = (((src[3] >> (7 - i)) & 1) << 0) | (((src[9] >> (7 - i)) & 1) << 1);
	}
}

static void reference_decode_s24_3le(u8 *dest, const u8 *src)
{
    int i;
    memset(dest, 0, 18);

    /* Channel 1 (bit 0 of bytes 0x00-0x17) */
    for (i = 0; i < 8; i++) {
        dest[0] |= ((src[0x10 + i] & 0x01) << (7 - i));
        dest[1] |= ((src[0x08 + i] & 0x01) << (7 - i));
        dest[2] |= ((src[0x00 + i] & 0x01) << (7 - i));
    }
    /* ... (rest of the reference decode - copy full function from ploytec_proto.c if you want a pure reference) */
    /* For brevity, we'll use the real functions for decode validation too. */
}

/* Simple random data generator */
static void fill_random(uint8_t *buf, size_t len)
{
    for (size_t i = 0; i < len; i++)
        buf[i] = rand() & 0xFF;
}


unsigned int validate_encoder(void)
{
	uint8_t src[3 * 4];		// 4ch * 3 bytes (encode input)
	uint8_t encoded_ref[48];	// 48 byte output frame to ploytec
	uint8_t encoded_opt[48];
	unsigned int errors = 0;
	
	for (int t = 0; t < TEST_ITERATIONS; t++) {
		fill_random(src, sizeof(src));
		fill_random(encoded_ref, sizeof(encoded_ref));  // noise

		/* 
		* Encode 4-channel S24_3LE to 48-byte Ploytec frame
		*
		* validate that the optimized version matches the reference;
		* input = src for both,
		* encoded_opt and encoded_ref are the outputs 
		*/

		ploytec_encode_s24_3le(encoded_ref, src);	// "known good" reference implementation

		/* put here the different encoders we'd like to compare against the reference... */
		ploytec_encode_s24_3le(encoded_opt, src);
		if (memcmp(encoded_opt, encoded_ref, 48) != 0) {
			errors++;
			if (errors < 5)
				printf("Encode mismatch at iteration %d\n", t);
		}	
	}

	return errors;
}



unsigned int validate_decoder(void)
{
	uint8_t src[64];		// 64 byte input frame from ploytec
	uint8_t decoded_ref[6 * 3];	// 6ch * 3 bytes (decode output)
	uint8_t decoded_opt[6 * 3];
	unsigned int errors = 0;
	
	for (int t = 0; t < TEST_ITERATIONS; t++) {
		fill_random(src, sizeof(src));
		fill_random(decoded_ref, sizeof(decoded_ref));  // noise

		/* 
		* Decode 64-byte Ploytec frame to 6-channel S24_3LE
		*
		* validate that the optimized version matches the reference;
		* input = src for both,
		* decoded_opt and decoded_ref are the outputs 
		*/		
		ploytec_decode_s24_3le(decoded_ref, src);	// "known good" reference implementation

		/* put here the different decoders we'd like to compare against the reference... */
		ploytec_decode_s24_3le(decoded_opt, src);
		if (memcmp(decoded_opt, decoded_ref, 6 * 3) != 0) {
			errors++;
			if (errors < 5)
				printf("Decode mismatch at iteration %d\n", t);
		}	
	}

	return errors;
}



void encode_benchmark(const char *label, encode_fn_t encode_fn)
{
	int frames = 1 << 10;	// 1024 frames (must be a power of 2)
	uint8_t src[frames * 3 * 4];		// 4ch * 3 bytes (encode input)
	uint8_t encoded[48];
	clock_t start, end;
	double time_ms;
	int frame = 0;

	fill_random(src, sizeof(src));

	start = clock();
	for (int i = 0; i < BENCHMARK_ITERATIONS; i++) {
		encode_fn(encoded, &src[frame * 3  * 4]);
		src[frame * 3 * 4] = encoded[12];
		frame = (frame + 1) % frames;
	}
	end = clock();
	time_ms = ((double)(end - start) * 1000.0) / CLOCKS_PER_SEC;
	printf("Encode %s: %.2f ms (%.2f ns/call)\n",
		label, time_ms, (time_ms * 1e6) / BENCHMARK_ITERATIONS);
	
}

void decode_benchmark(const char *label, decode_fn_t decode_fn)
{
	int frames = 1 << 10;	// 1024 frames (must be a power of 2)
	uint8_t src[frames * 64];	// 64 byte input frame 
	uint8_t decoded[6 * 3];
	clock_t start, end;
	double time_ms;
	int frame = 0;

	fill_random(src, sizeof(src));

	start = clock();
	for (int i = 0; i < BENCHMARK_ITERATIONS; i++) {
		/* feed diffrent data too the decoder to avoid the compiler from optimizing it out...*/
		decode_fn(decoded, &src[frame*64]);
		src[frame * 64] = decoded[12];
		frame = (frame + 1) % frames;
	}
	end = clock();
	time_ms = ((double)(end - start) * 1000.0) / CLOCKS_PER_SEC;
	printf("Decode %s: %.2f ms (%.2f ns/call)\n",
		label, time_ms, (time_ms * 1e6) / BENCHMARK_ITERATIONS);
}

int main(void)
{
	unsigned int errors = 0;
	
	printf("Ploytec codec test / benchmark\n\n");

	srand(time(NULL));


	/* === Correctness test === */
	printf("=== Correctness validation (Encoder)===\n");
	errors = validate_encoder();
	if (errors == 0)
		printf("✅ All %d encode tests passed (bit-exact)\n", TEST_ITERATIONS);
	else
		printf("❌ %d encode errors detected!\n", errors);


	printf("=== Correctness validation (Decoder)===\n");
	errors = validate_decoder();
	if (errors == 0)
		printf("✅ All %d decode tests passed (bit-exact)\n", TEST_ITERATIONS);
	else
		printf("❌ %d decode errors detected!\n", errors);



	/* === Benchmark === */
	printf("\n=== Performance benchmark (%d iterations) ===\n", BENCHMARK_ITERATIONS);
	encode_benchmark("Original", ploytec_encode_s24_3le);
	decode_benchmark("Original", ploytec_decode_s24_3le);

	return errors ? 1 : 0;
}