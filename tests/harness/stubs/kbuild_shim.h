/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Stand-in for what kbuild force-includes into every kernel source file.
 *
 * The driver's ploytec_codec.c selects between its three implementations with
 *
 *     #if IS_ENABLED(CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC)
 *     #elif defined(CONFIG_64BIT)
 *     #else
 *
 * but never includes <linux/kconfig.h> itself, because kbuild passes
 * '-include ./include/linux/kconfig.h' on every compile. Without an
 * equivalent here the user-space build hits a hard preprocessor error on the
 * IS_ENABLED line ("missing binary operator before token") and, because GCC
 * then treats the condition as false and CONFIG_64BIT is undefined too,
 * silently compiles the 32-bit variant - on every host, including x86_64.
 *
 * That is not hypothetical: it is what the previous test harness did, so its
 * recorded "driver" benchmark numbers were measuring the wrong codec.
 *
 * codecbench.py force-includes this file with -include, exactly as kbuild
 * would, and then defines the CONFIG_* symbols per variant on the command
 * line. The IS_ENABLED machinery below is copied verbatim from the kernel's
 * include/linux/kconfig.h so the selection behaves identically.
 */

#ifndef PLOYTEC_KBUILD_SHIM_H
#define PLOYTEC_KBUILD_SHIM_H

#define __ARG_PLACEHOLDER_1 0,
#define __take_second_arg(__ignored, val, ...) val

#define __or(x, y)			___or(x, y)
#define ___or(x, y)			____or(__ARG_PLACEHOLDER_##x, y)
#define ____or(arg1_or_junk, y)		__take_second_arg(arg1_or_junk 1, y)

#define __is_defined(x)			___is_defined(x)
#define ___is_defined(val)		____is_defined(__ARG_PLACEHOLDER_##val)
#define ____is_defined(arg1_or_junk)	__take_second_arg(arg1_or_junk 1, 0)

#define IS_BUILTIN(option) __is_defined(option)
#define IS_MODULE(option) __is_defined(option##_MODULE)
#define IS_ENABLED(option) __or(IS_BUILTIN(option), IS_MODULE(option))

#endif /* PLOYTEC_KBUILD_SHIM_H */
