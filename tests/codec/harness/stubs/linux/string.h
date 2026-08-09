/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Stub for <linux/string.h>. The codec only needs memset(), so forwarding to
 * the C library declaration is a faithful stand-in.
 *
 * The forward is unambiguous: the stub root is on the include path, but this
 * file lives in its linux/ subdirectory, so <string.h> resolves past the
 * stubs to the system header rather than back to here.
 */

#ifndef PLOYTEC_STUB_LINUX_STRING_H
#define PLOYTEC_STUB_LINUX_STRING_H

#include <string.h>

#endif /* PLOYTEC_STUB_LINUX_STRING_H */
