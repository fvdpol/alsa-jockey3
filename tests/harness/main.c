// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * User-space test bench for the Ploytec codec.
 *
 * Drives every implementation in the generated registry - the three driver
 * variants, built from a verbatim copy of ploytec_codec.c, plus anything in
 * candidates/ - through the same correctness bar and the same benchmark.
 *
 * Correctness is layered:
 *
 *   1. Golden vectors produced by tests/ploytec_model.py, an independent
 *      model of the wire format. This is the only layer that can catch the
 *      whole family being wrong together.
 *   2. Agreement with the driver's own portable reference codec over random
 *      frames, which is the regression net for the optimized variants.
 *   3. Property tests mirroring the in-kernel KUnit suite: the map is a
 *      complete injective bit permutation, it is linear over GF(2), playback
 *      leaves the reserved wire bits clear, capture ignores the unused wire
 *      bytes and bits, batches equal repeated single calls, a zero-frame
 *      batch writes nothing, and nothing is written outside the destination.
 *
 * A candidate has to clear all three before it deserves to be promoted into
 * the driver.
 *
 * (C) 2026 Frank van de Pol <fvdpol@gmail.com>
 */

#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <stdbool.h>
#include <time.h>
#include <math.h>

#include <linux/types.h>
#include "ploytec_codec.h"
#include "ploytec_codec_test_vectors.h"
#include "codec_impl.h"

#define ENC_PCM		PLOYTEC_PLAYBACK_PCM_FRAME_SIZE		/* 12 */
#define ENC_WIRE	PLOYTEC_PLAYBACK_FRAME_SIZE		/* 48 */
#define ENC_BATCH	PLOYTEC_PLAYBACK_FRAMES			/* 10 */
#define DEC_WIRE	PLOYTEC_CAPTURE_FRAME_SIZE		/* 64 */
#define DEC_PCM		PLOYTEC_CAPTURE_PCM_FRAME_SIZE		/* 18 */
#define DEC_BATCH	PLOYTEC_CAPTURE_FRAMES			/* 8 */

#define RANDOM_FRAMES		200000
#define LINEARITY_PAIRS		20000
#define GUARD			64
#define GUARD_BYTE		0xA5

/* Wire bytes the capture decoder must never read. */
#define UNUSED0_START	0x18
#define UNUSED0_END	0x20
#define UNUSED1_START	0x38
#define UNUSED1_END	0x40

/* Stop the compiler from optimizing away work whose result is unused. */
#define CONSUME(p)	__asm__ __volatile__("" :: "r"(p) : "memory")

/* --- deterministic PRNG, mirroring tests/ploytec_model.py ----------------- */

struct rng {
	u64 state;
};

static void rng_init(struct rng *r, u64 seed)
{
	r->state = seed ? seed : 1;
}

static u64 rng_next(struct rng *r)
{
	u64 x = r->state;

	x ^= x >> 12;
	x ^= x << 25;
	x ^= x >> 27;
	r->state = x;

	return x * 0x2545F4914F6CDD1DULL;
}

static void rng_fill(struct rng *r, u8 *buf, size_t len)
{
	size_t off = 0;

	while (off < len) {
		u64 v = rng_next(r);

		for (int i = 0; i < 8 && off < len; i++, off++)
			buf[off] = (v >> (i * 8)) & 0xFF;
	}
}

static bool capture_byte_used(int idx)
{
	if (idx >= UNUSED0_START && idx < UNUSED0_END)
		return false;
	if (idx >= UNUSED1_START && idx < UNUSED1_END)
		return false;

	return true;
}

static int count_set_bits(const u8 *buf, size_t len, int *last_pos)
{
	int count = 0;

	for (size_t i = 0; i < len; i++) {
		for (int b = 0; b < 8; b++) {
			if (buf[i] & (1u << b)) {
				count++;
				if (last_pos)
					*last_pos = (int)i * 8 + b;
			}
		}
	}

	return count;
}

/* --- reporting ------------------------------------------------------------ */

static int failures;
static bool json_output;

static void check_failed(const char *impl, const char *test, const char *fmt, ...)
{
	va_list ap;

	failures++;
	fprintf(stderr, "  FAIL %s / %s: ", impl, test);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
}

/* --- correctness ---------------------------------------------------------- */

static int test_golden_vectors(const struct ploytec_codec_impl *impl)
{
	u8 got_enc[ENC_WIRE], got_dec[DEC_PCM];
	int errors = 0;

	for (size_t i = 0; i < sizeof(ploytec_encode_vectors) /
				sizeof(ploytec_encode_vectors[0]); i++) {
		const struct ploytec_encode_vector *v = &ploytec_encode_vectors[i];

		memset(got_enc, 0xFF, sizeof(got_enc));
		impl->encode(got_enc, v->src, 1);
		if (memcmp(got_enc, v->expect, sizeof(got_enc)) != 0) {
			check_failed(impl->name, "golden-vectors",
				     "encode vector '%s' does not match the model",
				     v->name);
			errors++;
		}
	}

	for (size_t i = 0; i < sizeof(ploytec_decode_vectors) /
				sizeof(ploytec_decode_vectors[0]); i++) {
		const struct ploytec_decode_vector *v = &ploytec_decode_vectors[i];

		memset(got_dec, 0xFF, sizeof(got_dec));
		impl->decode(got_dec, v->src, 1);
		if (memcmp(got_dec, v->expect, sizeof(got_dec)) != 0) {
			check_failed(impl->name, "golden-vectors",
				     "decode vector '%s' does not match the model",
				     v->name);
			errors++;
		}
	}

	return errors;
}

static int test_against_reference(const struct ploytec_codec_impl *impl,
				  const struct ploytec_codec_impl *ref)
{
	u8 src_e[ENC_PCM], got_e[ENC_WIRE], want_e[ENC_WIRE];
	u8 src_d[DEC_WIRE], got_d[DEC_PCM], want_d[DEC_PCM];
	struct rng rng;
	int errors = 0;

	if (impl == ref)
		return 0;

	rng_init(&rng, 0x0123456789ABCDEFULL);

	for (int t = 0; t < RANDOM_FRAMES; t++) {
		rng_fill(&rng, src_e, sizeof(src_e));
		memset(got_e, 0x5A, sizeof(got_e));
		impl->encode(got_e, src_e, 1);
		ref->encode(want_e, src_e, 1);
		if (memcmp(got_e, want_e, sizeof(got_e)) != 0) {
			check_failed(impl->name, "vs-reference",
				     "encode differs at iteration %d", t);
			errors++;
			break;
		}
	}

	rng_init(&rng, 0x0123456789ABCDEFULL);

	for (int t = 0; t < RANDOM_FRAMES; t++) {
		rng_fill(&rng, src_d, sizeof(src_d));
		memset(got_d, 0x5A, sizeof(got_d));
		impl->decode(got_d, src_d, 1);
		ref->decode(want_d, src_d, 1);
		if (memcmp(got_d, want_d, sizeof(got_d)) != 0) {
			check_failed(impl->name, "vs-reference",
				     "decode differs at iteration %d", t);
			errors++;
			break;
		}
	}

	return errors;
}

/*
 * The codec is a bit permutation, so it is linear over GF(2) and is fully
 * determined by its action on the basis vectors. Enumerating the one-hot
 * inputs measures that action; the linearity check below establishes that the
 * determination is valid for every other input.
 */
static int test_permutation(const struct ploytec_codec_impl *impl)
{
	u8 src_e[ENC_PCM], dst_e[ENC_WIRE];
	u8 src_d[DEC_WIRE], dst_d[DEC_PCM];
	bool seen_e[ENC_WIRE * 8] = { false };
	bool seen_d[DEC_PCM * 8] = { false };
	int errors = 0;

	for (int byte = 0; byte < ENC_PCM; byte++) {
		for (int bit = 0; bit < 8; bit++) {
			int pos = -1, count;

			memset(src_e, 0, sizeof(src_e));
			src_e[byte] = 1u << bit;
			memset(dst_e, 0, sizeof(dst_e));
			impl->encode(dst_e, src_e, 1);

			count = count_set_bits(dst_e, sizeof(dst_e), &pos);
			if (count != 1) {
				check_failed(impl->name, "permutation",
					     "encode src[%d] bit %d set %d output bits, expected 1",
					     byte, bit, count);
				errors++;
			} else if (seen_e[pos]) {
				check_failed(impl->name, "permutation",
					     "encode output bit %d written by two input bits",
					     pos);
				errors++;
			} else {
				seen_e[pos] = true;
			}
		}
	}

	for (int byte = 0; byte < DEC_WIRE; byte++) {
		for (int bit = 0; bit < 8; bit++) {
			bool meaningful = capture_byte_used(byte) && bit < 3;
			int pos = -1, count;

			memset(src_d, 0, sizeof(src_d));
			src_d[byte] = 1u << bit;
			memset(dst_d, 0, sizeof(dst_d));
			impl->decode(dst_d, src_d, 1);

			count = count_set_bits(dst_d, sizeof(dst_d), &pos);

			if (!meaningful) {
				if (count != 0) {
					check_failed(impl->name, "permutation",
						     "decode wire byte %#04x bit %d is unused but changed the output",
						     byte, bit);
					errors++;
				}
				continue;
			}

			if (count != 1) {
				check_failed(impl->name, "permutation",
					     "decode wire byte %#04x bit %d set %d output bits, expected 1",
					     byte, bit, count);
				errors++;
			} else if (seen_d[pos]) {
				check_failed(impl->name, "permutation",
					     "decode output bit %d written by two wire bits",
					     pos);
				errors++;
			} else {
				seen_d[pos] = true;
			}
		}
	}

	return errors;
}

static int test_linearity(const struct ploytec_codec_impl *impl)
{
	u8 a[DEC_WIRE], b[DEC_WIRE], ab[DEC_WIRE];
	u8 fa[ENC_WIRE], fb[ENC_WIRE], fab[ENC_WIRE], want[ENC_WIRE];
	struct rng rng;
	int errors = 0;

	rng_init(&rng, 0xFEEDFACECAFEBEEFULL);

	for (int t = 0; t < LINEARITY_PAIRS; t++) {
		rng_fill(&rng, a, ENC_PCM);
		rng_fill(&rng, b, ENC_PCM);
		for (int i = 0; i < ENC_PCM; i++)
			ab[i] = a[i] ^ b[i];

		impl->encode(fa, a, 1);
		impl->encode(fb, b, 1);
		impl->encode(fab, ab, 1);
		for (int i = 0; i < ENC_WIRE; i++)
			want[i] = fa[i] ^ fb[i];

		if (memcmp(fab, want, ENC_WIRE) != 0) {
			check_failed(impl->name, "linearity",
				     "encode(a^b) != encode(a)^encode(b) at iteration %d",
				     t);
			errors++;
			break;
		}
	}

	for (int t = 0; t < LINEARITY_PAIRS; t++) {
		u8 da[DEC_PCM], db[DEC_PCM], dab[DEC_PCM], dwant[DEC_PCM];

		rng_fill(&rng, a, DEC_WIRE);
		rng_fill(&rng, b, DEC_WIRE);
		for (int i = 0; i < DEC_WIRE; i++)
			ab[i] = a[i] ^ b[i];

		impl->decode(da, a, 1);
		impl->decode(db, b, 1);
		impl->decode(dab, ab, 1);
		for (int i = 0; i < DEC_PCM; i++)
			dwant[i] = da[i] ^ db[i];

		if (memcmp(dab, dwant, DEC_PCM) != 0) {
			check_failed(impl->name, "linearity",
				     "decode(a^b) != decode(a)^decode(b) at iteration %d",
				     t);
			errors++;
			break;
		}
	}

	return errors;
}

static int test_invariants(const struct ploytec_codec_impl *impl)
{
	u8 src_e[ENC_PCM], dst_e[ENC_WIRE];
	u8 src_d[DEC_WIRE], probe[DEC_WIRE], want[DEC_PCM], got[DEC_PCM];
	struct rng rng;
	int errors = 0;

	/* Silence encodes to an all-zero wire frame. */
	memset(src_e, 0, sizeof(src_e));
	memset(dst_e, 0xFF, sizeof(dst_e));
	impl->encode(dst_e, src_e, 1);
	for (int i = 0; i < ENC_WIRE; i++) {
		if (dst_e[i] != 0) {
			check_failed(impl->name, "invariants",
				     "silence did not encode to zero (wire byte %d = %#04x)",
				     i, dst_e[i]);
			errors++;
			break;
		}
	}

	/*
	 * Playback packs two channels per wire byte, so bits 2..7 must stay
	 * clear - jockey3.c writes MIDI and sync bytes into the same packet.
	 */
	rng_init(&rng, 0x0F0F0F0FF0F0F0F0ULL);
	memset(src_e, 0xFF, sizeof(src_e));
	for (int t = 0; t < 4096; t++) {
		memset(dst_e, 0, sizeof(dst_e));
		impl->encode(dst_e, src_e, 1);
		for (int i = 0; i < ENC_WIRE; i++) {
			if (dst_e[i] & 0xFC) {
				check_failed(impl->name, "invariants",
					     "wire byte %d has reserved bits set (%#04x)",
					     i, dst_e[i]);
				errors++;
				t = 4096;
				break;
			}
		}
		rng_fill(&rng, src_e, sizeof(src_e));
	}

	/* Capture must ignore the unused wire bytes and the unused bits. */
	rng_init(&rng, 0xABCDEF0123456789ULL);
	rng_fill(&rng, src_d, sizeof(src_d));
	impl->decode(want, src_d, 1);

	for (int t = 0; t < 4096; t++) {
		memcpy(probe, src_d, sizeof(probe));
		for (int i = 0; i < DEC_WIRE; i++) {
			if (!capture_byte_used(i))
				probe[i] = rng_next(&rng) & 0xFF;
			else
				probe[i] = (src_d[i] & 0x07) |
					   (rng_next(&rng) & 0xF8);
		}

		impl->decode(got, probe, 1);
		if (memcmp(got, want, DEC_PCM) != 0) {
			check_failed(impl->name, "invariants",
				     "decode changed when only unused wire bytes/bits changed");
			errors++;
			break;
		}
	}

	return errors;
}

static int test_batching(const struct ploytec_codec_impl *impl)
{
	u8 src_e[ENC_BATCH * ENC_PCM], src_d[DEC_BATCH * DEC_WIRE];
	u8 batched_e[ENC_BATCH * ENC_WIRE], single_e[ENC_BATCH * ENC_WIRE];
	u8 batched_d[DEC_BATCH * DEC_PCM], single_d[DEC_BATCH * DEC_PCM];
	u8 guarded[GUARD * 2 + ENC_BATCH * ENC_WIRE];
	struct rng rng;
	int errors = 0;

	rng_init(&rng, 0xD1CE0FF1CE0FF1CEULL);
	rng_fill(&rng, src_e, sizeof(src_e));
	rng_fill(&rng, src_d, sizeof(src_d));

	for (int n = 1; n <= ENC_BATCH; n++) {
		memset(batched_e, 0, sizeof(batched_e));
		memset(single_e, 0, sizeof(single_e));

		impl->encode(batched_e, src_e, n);
		for (int f = 0; f < n; f++)
			impl->encode(single_e + f * ENC_WIRE,
				     src_e + f * ENC_PCM, 1);

		if (memcmp(batched_e, single_e, (size_t)n * ENC_WIRE) != 0) {
			check_failed(impl->name, "batching",
				     "encode batch of %d differs from %d single calls",
				     n, n);
			errors++;
		}
	}

	for (int n = 1; n <= DEC_BATCH; n++) {
		memset(batched_d, 0, sizeof(batched_d));
		memset(single_d, 0, sizeof(single_d));

		impl->decode(batched_d, src_d, n);
		for (int f = 0; f < n; f++)
			impl->decode(single_d + f * DEC_PCM,
				     src_d + f * DEC_WIRE, 1);

		if (memcmp(batched_d, single_d, (size_t)n * DEC_PCM) != 0) {
			check_failed(impl->name, "batching",
				     "decode batch of %d differs from %d single calls",
				     n, n);
			errors++;
		}
	}

	/*
	 * jockey3.c derives the batch size from the ALSA ring-buffer wrap and
	 * can legitimately arrive at zero.
	 */
	memset(batched_e, GUARD_BYTE, sizeof(batched_e));
	impl->encode(batched_e, src_e, 0);
	for (size_t i = 0; i < sizeof(batched_e); i++) {
		if (batched_e[i] != GUARD_BYTE) {
			check_failed(impl->name, "batching",
				     "encode of 0 frames wrote to dest[%zu]", i);
			errors++;
			break;
		}
	}

	memset(batched_d, GUARD_BYTE, sizeof(batched_d));
	impl->decode(batched_d, src_d, 0);
	for (size_t i = 0; i < sizeof(batched_d); i++) {
		if (batched_d[i] != GUARD_BYTE) {
			check_failed(impl->name, "batching",
				     "decode of 0 frames wrote to dest[%zu]", i);
			errors++;
			break;
		}
	}

	/* Nothing may be written outside the destination buffer. */
	memset(guarded, GUARD_BYTE, sizeof(guarded));
	impl->encode(guarded + GUARD, src_e, ENC_BATCH);
	for (int i = 0; i < GUARD; i++) {
		if (guarded[i] != GUARD_BYTE ||
		    guarded[GUARD + ENC_BATCH * ENC_WIRE + i] != GUARD_BYTE) {
			check_failed(impl->name, "batching",
				     "encode wrote outside the destination buffer");
			errors++;
			break;
		}
	}

	return errors;
}

/*
 * The optimized codecs use the unaligned accessors; a misuse only faults on
 * strict-alignment machines, so sweep every offset explicitly here too.
 */
static int test_alignment(const struct ploytec_codec_impl *impl)
{
	u8 src_e[ENC_PCM], ref_e[ENC_WIRE];
	u8 src_d[DEC_WIRE], ref_d[DEC_PCM];
	u8 src_buf[DEC_WIRE + 8], dst_buf[ENC_WIRE + 8];
	struct rng rng;
	int errors = 0;

	rng_init(&rng, 0x1234ABCD5678EF90ULL);
	rng_fill(&rng, src_e, sizeof(src_e));
	rng_fill(&rng, src_d, sizeof(src_d));

	impl->encode(ref_e, src_e, 1);
	impl->decode(ref_d, src_d, 1);

	for (int so = 0; so < 8 && !errors; so++) {
		for (int doff = 0; doff < 8; doff++) {
			memcpy(src_buf + so, src_e, sizeof(src_e));
			memset(dst_buf, 0, sizeof(dst_buf));
			impl->encode(dst_buf + doff, src_buf + so, 1);
			if (memcmp(dst_buf + doff, ref_e, ENC_WIRE) != 0) {
				check_failed(impl->name, "alignment",
					     "encode differs at src offset %d, dst offset %d",
					     so, doff);
				errors++;
				break;
			}

			memcpy(src_buf + so, src_d, sizeof(src_d));
			memset(dst_buf, 0, sizeof(dst_buf));
			impl->decode(dst_buf + doff, src_buf + so, 1);
			if (memcmp(dst_buf + doff, ref_d, DEC_PCM) != 0) {
				check_failed(impl->name, "alignment",
					     "decode differs at src offset %d, dst offset %d",
					     so, doff);
				errors++;
				break;
			}
		}
	}

	return errors;
}

static const struct ploytec_codec_impl *find_reference(void)
{
	for (unsigned int i = 0; i < ploytec_impl_count; i++) {
		if (strcmp(ploytec_impls[i].name, ploytec_reference_impl) == 0)
			return &ploytec_impls[i];
	}

	return NULL;
}

static int run_correctness(const char *only)
{
	const struct ploytec_codec_impl *ref = find_reference();

	if (!ref) {
		fprintf(stderr, "no reference implementation '%s' in the registry\n",
			ploytec_reference_impl);
		return 1;
	}

	printf("Correctness (oracle: golden vectors from the Python model, plus '%s')\n\n",
	       ref->name);

	for (unsigned int i = 0; i < ploytec_impl_count; i++) {
		const struct ploytec_codec_impl *impl = &ploytec_impls[i];
		int before = failures;

		if (only && strcmp(only, impl->name) != 0)
			continue;

		printf("  %-18s %-10s ", impl->name,
		       impl->kind == PLOYTEC_IMPL_DRIVER ? "[driver]" : "[candidate]");
		fflush(stdout);

		test_golden_vectors(impl);
		test_against_reference(impl, ref);
		test_permutation(impl);
		test_linearity(impl);
		test_invariants(impl);
		test_batching(impl);
		test_alignment(impl);

		if (failures == before)
			printf("PASS\n");
		else
			printf("FAIL (%d)\n", failures - before);
	}

	printf("\n");

	return failures ? 1 : 0;
}

/* --- benchmarking --------------------------------------------------------- */

struct bench_opts {
	double duration_s;	/* target seconds per measurement */
	int repeats;
	double calibrate_s;
};

static double now_s(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);

	return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static int cmp_double(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;

	return (x > y) - (x < y);
}

/*
 * A ring of distinct input frames, so the benchmark is not measuring a single
 * cache-resident frame, and a clobber barrier on the output so the work
 * cannot be optimized away. The previous harness fed an output byte back into
 * the input to defeat dead-code elimination, which also changed the data
 * being measured; the barrier does the job without that side effect.
 */
#define BENCH_RING 1024

struct bench_result {
	double ns_per_call;
	double rsd_percent;
	double elapsed_s;
	long iterations;
};

static double bench_once(ploytec_encode_fn fn, u8 *dst, const u8 *ring,
			 size_t stride, int n_frames, long iterations)
{
	double start = now_s();
	size_t idx = 0;

	for (long i = 0; i < iterations; i++) {
		fn(dst, ring + idx * stride, n_frames);
		CONSUME(dst);
		idx = (idx + 1) & (BENCH_RING - 1);
	}

	return now_s() - start;
}

static struct bench_result bench_fn(ploytec_encode_fn fn, u8 *dst,
				    const u8 *ring, size_t stride, int n_frames,
				    const struct bench_opts *opts)
{
	struct bench_result res = { 0 };
	double *samples;
	double calib, total = 0, mean = 0, var = 0;
	long iterations;

	/* Warm up caches and branch predictors before timing anything. */
	bench_once(fn, dst, ring, stride, n_frames, 1000);

	/* Calibrate: how long does one call actually take here? */
	calib = bench_once(fn, dst, ring, stride, n_frames, 10000);
	if (calib <= 0)
		calib = 1e-6;
	iterations = (long)(10000.0 * (opts->calibrate_s / calib));
	if (iterations < 1000)
		iterations = 1000;

	calib = bench_once(fn, dst, ring, stride, n_frames, iterations);
	if (calib <= 0)
		calib = 1e-9;

	/* Size the real runs to hit the requested duration. */
	iterations = (long)((double)iterations * (opts->duration_s / calib));
	if (iterations < 1000)
		iterations = 1000;

	samples = calloc((size_t)opts->repeats, sizeof(*samples));
	if (!samples)
		return res;

	for (int r = 0; r < opts->repeats; r++) {
		double elapsed = bench_once(fn, dst, ring, stride, n_frames,
					    iterations);

		samples[r] = elapsed * 1e9 / ((double)iterations * n_frames);
		total += elapsed;
		mean += samples[r];
	}

	mean /= opts->repeats;
	for (int r = 0; r < opts->repeats; r++)
		var += (samples[r] - mean) * (samples[r] - mean);
	var /= opts->repeats;

	qsort(samples, (size_t)opts->repeats, sizeof(*samples), cmp_double);

	res.ns_per_call = samples[opts->repeats / 2];
	res.rsd_percent = mean > 0 ? 100.0 * sqrt(var) / mean : 0.0;
	res.elapsed_s = total;
	res.iterations = iterations;

	free(samples);

	return res;
}

static void run_benchmark(const struct bench_opts *opts, const char *only)
{
	static u8 enc_ring[BENCH_RING * ENC_BATCH * ENC_PCM];
	static u8 dec_ring[BENCH_RING * DEC_BATCH * DEC_WIRE];
	static u8 enc_dst[ENC_BATCH * ENC_WIRE];
	static u8 dec_dst[DEC_BATCH * DEC_PCM];
	struct rng rng;
	int counted = 0;

	rng_init(&rng, 0xC0FFEE0123456789ULL);
	rng_fill(&rng, enc_ring, sizeof(enc_ring));
	rng_fill(&rng, dec_ring, sizeof(dec_ring));

	for (unsigned int i = 0; i < ploytec_impl_count; i++) {
		if (!only || strcmp(only, ploytec_impls[i].name) == 0)
			counted++;
	}

	fprintf(stderr,
		"Benchmarking %d implementation(s), %d repeats of ~%.0fs each per direction.\n"
		"Estimated run time: ~%.0f minutes.\n\n",
		counted, opts->repeats, opts->duration_s,
		(counted * 2.0 * opts->repeats * opts->duration_s) / 60.0);

	if (!json_output) {
		printf("%-18s %-10s %12s %12s %8s %8s\n",
		       "IMPLEMENTATION", "KIND", "ENC ns/frame", "DEC ns/frame",
		       "ENC RSD", "DEC RSD");
	} else {
		printf("{\n  \"results\": [\n");
	}

	for (unsigned int i = 0; i < ploytec_impl_count; i++) {
		const struct ploytec_codec_impl *impl = &ploytec_impls[i];
		struct bench_result enc, dec;

		if (only && strcmp(only, impl->name) != 0)
			continue;

		enc = bench_fn(impl->encode, enc_dst, enc_ring,
			       ENC_BATCH * ENC_PCM, ENC_BATCH, opts);
		dec = bench_fn(impl->decode, dec_dst, dec_ring,
			       DEC_BATCH * DEC_WIRE, DEC_BATCH, opts);

		if (json_output) {
			printf("    {\"name\": \"%s\", \"kind\": \"%s\", "
			       "\"encode_ns_per_frame\": %.3f, \"decode_ns_per_frame\": %.3f, "
			       "\"encode_rsd_percent\": %.2f, \"decode_rsd_percent\": %.2f, "
			       "\"encode_iterations\": %ld, \"decode_iterations\": %ld}%s\n",
			       impl->name,
			       impl->kind == PLOYTEC_IMPL_DRIVER ? "driver" : "candidate",
			       enc.ns_per_call, dec.ns_per_call,
			       enc.rsd_percent, dec.rsd_percent,
			       enc.iterations, dec.iterations,
			       i + 1 < ploytec_impl_count ? "," : "");
		} else {
			printf("%-18s %-10s %12.2f %12.2f %7.1f%% %7.1f%%%s\n",
			       impl->name,
			       impl->kind == PLOYTEC_IMPL_DRIVER ? "driver" : "candidate",
			       enc.ns_per_call, dec.ns_per_call,
			       enc.rsd_percent, dec.rsd_percent,
			       (enc.rsd_percent > 5.0 || dec.rsd_percent > 5.0) ?
			       "  <- noisy, raise --duration" : "");
		}
		fflush(stdout);
	}

	if (json_output)
		printf("  ]\n}\n");
	else
		printf("\nns/frame is per PCM frame, measured through the batch API at the\n"
		       "driver's real batch sizes (%d encode, %d decode).\n",
		       ENC_BATCH, DEC_BATCH);
}

/* --- main ----------------------------------------------------------------- */

static void usage(void)
{
	printf("usage: test_codec <test|bench|list> [options]\n"
	       "  --duration SEC   target seconds per measurement (default 30)\n"
	       "  --repeats N      measurements per implementation (default 5)\n"
	       "  --only NAME      restrict to one implementation\n"
	       "  --json           emit JSON (bench only)\n");
}

int main(int argc, char **argv)
{
	struct bench_opts opts = { .duration_s = 30.0, .repeats = 5,
				   .calibrate_s = 0.05 };
	const char *only = NULL;
	const char *cmd;

	if (argc < 2) {
		usage();
		return 2;
	}

	cmd = argv[1];

	for (int i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "--duration") && i + 1 < argc)
			opts.duration_s = atof(argv[++i]);
		else if (!strcmp(argv[i], "--repeats") && i + 1 < argc)
			opts.repeats = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--only") && i + 1 < argc)
			only = argv[++i];
		else if (!strcmp(argv[i], "--json"))
			json_output = true;
		else {
			usage();
			return 2;
		}
	}

	if (opts.repeats < 1)
		opts.repeats = 1;

	for (unsigned int i = 0; i < ploytec_impl_count; i++) {
		if (ploytec_impls[i].init)
			ploytec_impls[i].init();
	}

	if (!strcmp(cmd, "list")) {
		for (unsigned int i = 0; i < ploytec_impl_count; i++)
			printf("%-18s %-10s %s\n", ploytec_impls[i].name,
			       ploytec_impls[i].kind == PLOYTEC_IMPL_DRIVER ?
			       "driver" : "candidate",
			       ploytec_impls[i].description);
		return 0;
	}

	if (!strcmp(cmd, "test"))
		return run_correctness(only);

	if (!strcmp(cmd, "bench")) {
		run_benchmark(&opts, only);
		return 0;
	}

	usage();

	return 2;
}
