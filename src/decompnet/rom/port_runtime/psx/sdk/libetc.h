#ifndef PSX_LIBETC_H
#define PSX_LIBETC_H
#include "psx_sdk_types.h"
#include "libgpu.h" /* psx_addr */
int VSync(int mode);
psx_addr VSyncCallback(psx_addr func);
psx_addr VSyncCallbacks(int ch, psx_addr func);
psx_addr InterruptCallback(int irq, psx_addr func);
psx_addr DMACallback(int ch, psx_addr func);
int ResetCallback(void);
void StopCallback(void);
int RestartCallback(void);
int CheckCallback(void);
int SetVideoMode(int mode);
int GetVideoMode(void);
void PadInit(int mode);
uint32_t PadRead(int id);
void PadStop(void);
int SetRCnt(uint32_t spec, int target, int mode);
int GetRCnt(uint32_t spec);
int StartRCnt(uint32_t spec);
int StopRCnt(uint32_t spec);
int ResetRCnt(uint32_t spec);
#endif
