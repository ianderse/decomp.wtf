#ifndef PSX_LIBGPU_H
#define PSX_LIBGPU_H
#include "psx_sdk_types.h"
int ResetGraph(int mode);
int SetGraphDebug(int level);
void SetDispMask(int mask);
DRAWENV *SetDefDrawEnv(DRAWENV *env, int x, int y, int w, int h);
DISPENV *SetDefDispEnv(DISPENV *env, int x, int y, int w, int h);
DRAWENV *PutDrawEnv(DRAWENV *env);
DISPENV *PutDispEnv(DISPENV *env);
/* what the last PutDrawEnv asked the screen to be cleared to (isbg), for the window */
void psx_gpu_clear_color(uint8_t *r, uint8_t *g, uint8_t *b);
#endif
