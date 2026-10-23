/* Force-included (-include) in every one of the game's files, before anything else. */
#ifndef PSX_PORT_GAME_H
#define PSX_PORT_GAME_H
/* gnu89 `extern inline` has no out-of-line copy: GCC 2.x always inlined it, clang may not (an undefined symbol at
 * link). Every inline function in the game's files is inlined, as it was. */
#define __inline__ __inline__ __attribute__((__always_inline__))
#define __inline __inline__
#define inline __inline__
/* the host's GTE macros; common.h's own #include "psx_gte.h" then finds the guard defined */
#include "psx_gte.h"
#endif
