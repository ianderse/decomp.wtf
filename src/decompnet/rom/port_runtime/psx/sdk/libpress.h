/* libpress over the software MDEC (psx/mdec.c): sdk/libpress.c. Pointers are the game's PS1 addresses (uint32_t),
 * callbacks PS1 function addresses. */
#ifndef PSX_LIBPRESS_H
#define PSX_LIBPRESS_H
#include <stdint.h>
#include "psx_sdk_types.h"

/* DecDCTGetEnv/DecDCTPutEnv's environment, as PSY-Q's DECDCTENV */
typedef struct {
    uint8_t iq_y[64]; /* luminance quant table, zigzag order */
    uint8_t iq_c[64]; /* colour quant table */
    int16_t dct[64];  /* IDCT scale matrix */
} PsxDecDctEnv;

void DecDCTReset(int mode);
void MDEC_reset(int mode);
uint32_t DecDCTGetEnv(uint32_t env);
uint32_t DecDCTPutEnv(uint32_t env);
int DecDCTBufSize(uint32_t bs);
int DecDCTvlc(uint32_t bs, uint32_t buf);
int DecDCTvlcSize(int size);
int DecDCTvlc2(uint32_t bs, uint32_t buf, uint32_t table);
int DecDCTvlcSize2(int size);
void DecDCTvlcBuild(uint32_t table);
void DecDCTin(uint32_t buf, int mode);
void DecDCTout(uint32_t buf, int size);
int DecDCTinSync(int mode);
int DecDCToutSync(int mode);
uint32_t DecDCTinCallback(uintptr_t func); /* a PS1 address, or a game function (all 64 bits) */
uint32_t DecDCToutCallback(uintptr_t func);

/* the run's MDEC work, for the exit report (psx_exit_report) */
void psx_mdec_exit(void);

#endif
