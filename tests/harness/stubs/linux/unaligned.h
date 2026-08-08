/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Stub for <linux/unaligned.h>.
 *
 * The codec reads and writes 32- and 64-bit words through these accessors,
 * which is what makes it correct on big-endian hosts and on architectures
 * that fault on unaligned access. memcpy() is the portable way to express an
 * unaligned load in user space; compilers turn it into a single instruction
 * where the hardware allows one, so this does not distort benchmarks.
 *
 * Note the driver moved from <asm/unaligned.h> to <linux/unaligned.h>; both
 * paths are provided so the harness can still build older revisions of the
 * codec when bisecting.
 */

#ifndef PLOYTEC_STUB_LINUX_UNALIGNED_H
#define PLOYTEC_STUB_LINUX_UNALIGNED_H

#include <string.h>
#include <linux/types.h>

#if defined(__BYTE_ORDER__) && defined(__ORDER_BIG_ENDIAN__) && \
	(__BYTE_ORDER__ == __ORDER_BIG_ENDIAN__)
#define PLOYTEC_STUB_BIG_ENDIAN 1
#else
#define PLOYTEC_STUB_BIG_ENDIAN 0
#endif

static inline u32 get_unaligned_le32(const void *p)
{
	u32 val;

	memcpy(&val, p, sizeof(val));
#if PLOYTEC_STUB_BIG_ENDIAN
	return __builtin_bswap32(val);
#else
	return val;
#endif
}

static inline void put_unaligned_le32(u32 val, void *p)
{
#if PLOYTEC_STUB_BIG_ENDIAN
	val = __builtin_bswap32(val);
#endif
	memcpy(p, &val, sizeof(val));
}

static inline u64 get_unaligned_le64(const void *p)
{
	u64 val;

	memcpy(&val, p, sizeof(val));
#if PLOYTEC_STUB_BIG_ENDIAN
	return __builtin_bswap64(val);
#else
	return val;
#endif
}

static inline void put_unaligned_le64(u64 val, void *p)
{
#if PLOYTEC_STUB_BIG_ENDIAN
	val = __builtin_bswap64(val);
#endif
	memcpy(p, &val, sizeof(val));
}

#endif /* PLOYTEC_STUB_LINUX_UNALIGNED_H */
