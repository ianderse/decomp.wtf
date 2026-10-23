/* PSY-Q's structs with the PS1's layout: `long` is 32 bits there, so it is int32_t here (on a 64-bit host `long` is
 * 8 bytes, and a struct the game built in RAM would be read wrong). gen/psx_sdk_config.h says which of them the game's
 * common.h already defines (PSX_COMMON_HAS_<NAME>): a file that includes common.h too defines PSX_WITH_COMMON_H first,
 * and then those come from common.h. */
#ifndef PSX_SDK_TYPES_H
#define PSX_SDK_TYPES_H
#include <stdint.h>

#include "psx_sdk_config.h"

#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_RECT)
typedef struct { int16_t x, y, w, h; } RECT;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_DR_ENV)
typedef struct { uint32_t tag; uint32_t code[15]; } DR_ENV;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_DRAWENV)
typedef struct {
    RECT clip;
    int16_t ofs[2];
    RECT tw;
    uint16_t tpage;
    uint8_t dtd, dfe, isbg, r0, g0, b0;
    DR_ENV dr_env;
} DRAWENV;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_DISPENV)
typedef struct { RECT disp; RECT screen; uint8_t isinter, isrgb24, pad0, pad1; } DISPENV;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_MATRIX)
typedef struct { int16_t m[3][3]; int32_t t[3]; } MATRIX;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_SVECTOR)
typedef struct { int16_t vx, vy, vz, pad; } SVECTOR;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_VECTOR)
typedef struct { int32_t vx, vy, vz, pad; } VECTOR;
#endif
#if !defined(PSX_WITH_COMMON_H) || !defined(PSX_COMMON_HAS_CVECTOR)
typedef struct { uint8_t r, g, b, cd; } CVECTOR;
#endif
#endif
