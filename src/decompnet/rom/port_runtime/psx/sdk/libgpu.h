/* libgpu over the software GPU (psx/gpu.c). Pointer parameters are the game's PS1 addresses (uint32_t: only the low
 * 32 bits of the register mean anything); every function that returns a pointer returns the PS1 address it was given. */
#ifndef PSX_LIBGPU_H
#define PSX_LIBGPU_H
#include "psx_sdk_types.h"
typedef uint32_t psx_addr; /* a PS1 address */

int ResetGraph(int mode);
int SetGraphDebug(int level);
int SetGraphReverse(int mode);
int SetGraphQueue(int mode);
int GetGraphType(void);
int GetGraphDebug(void);
void SetDispMask(int mask);
psx_addr SetDefDrawEnv(psx_addr env, int x, int y, int w, int h);
psx_addr SetDefDispEnv(psx_addr env, int x, int y, int w, int h);
psx_addr PutDrawEnv(psx_addr env);
psx_addr PutDispEnv(psx_addr env);
psx_addr GetDrawEnv(psx_addr env);
psx_addr GetDispEnv(psx_addr env);
int DrawSync(int mode);
psx_addr DrawSyncCallback(psx_addr func);
psx_addr ClearOTag(psx_addr ot, int n);
psx_addr ClearOTagR(psx_addr ot, int n);
void AddPrim(psx_addr ot, psx_addr p);
void AddPrims(psx_addr ot, psx_addr p0, psx_addr p1);
void CatPrim(psx_addr p0, psx_addr p1);
void TermPrim(psx_addr p);
void DrawOTag(psx_addr p);
void DrawOTagIO(psx_addr p);
void DrawOTagEnv(psx_addr p, psx_addr env);
void DrawPrim(psx_addr p);
int LoadImage(psx_addr rect, psx_addr p);
int LoadImage2(psx_addr rect, psx_addr p);
int StoreImage(psx_addr rect, psx_addr p);
int StoreImage2(psx_addr rect, psx_addr p);
int MoveImage(psx_addr rect, int x, int y);
int ClearImage(psx_addr rect, int r, int g, int b);
int ClearImage2(psx_addr rect, int r, int g, int b);
int LoadTPage(psx_addr pix, int tp, int abr, int x, int y, int w, int h);
int LoadClut(psx_addr clut, int x, int y);
int LoadClut2(psx_addr clut, int x, int y);
int GetTPage(int tp, int abr, int x, int y);
int GetClut(int x, int y);
void SetDrawEnv(psx_addr dr_env, psx_addr env);
void SetDrawMode(psx_addr p, int dfe, int dtd, int tpage, psx_addr tw);
void SetDrawTPage(psx_addr p, int dfe, int dtd, int tpage);
void SetDrawArea(psx_addr p, psx_addr rect);
void SetDrawOffset(psx_addr p, psx_addr ofs);
void SetDrawStp(psx_addr p, int pbw);
void SetTexWindow(psx_addr p, psx_addr tw);
void SetDrawMove(psx_addr p, psx_addr rect, int x, int y);
void SetPriority(psx_addr p, int pbc, int pbw);
uint32_t get_mode(int dfe, int dtd, int tpage);
uint32_t get_cs(int x, int y);
uint32_t get_ce(int x, int y);
uint32_t get_ofs(int x, int y);
uint32_t get_tw(psx_addr tw);
int get_dx(psx_addr env);
/* the primitive setters: the length and code bytes of the game's primitive */
void SetPolyF3(psx_addr p); void SetPolyFT3(psx_addr p); void SetPolyG3(psx_addr p); void SetPolyGT3(psx_addr p);
void SetPolyF4(psx_addr p); void SetPolyFT4(psx_addr p); void SetPolyG4(psx_addr p); void SetPolyGT4(psx_addr p);
void SetSprt(psx_addr p); void SetSprt8(psx_addr p); void SetSprt16(psx_addr p);
void SetTile(psx_addr p); void SetTile1(psx_addr p); void SetTile8(psx_addr p); void SetTile16(psx_addr p);
void SetLineF2(psx_addr p); void SetLineG2(psx_addr p); void SetLineF3(psx_addr p); void SetLineG3(psx_addr p);
void SetLineF4(psx_addr p); void SetLineG4(psx_addr p);
void SetSemiTrans(psx_addr p, int abe);
void SetShadeTex(psx_addr p, int tge);
/* the driver under libgpu (the table its upper half calls through: D_801A1480 in SLUS-01252) */
int _addque(psx_addr func, psx_addr param, int arg);
int _addque2(psx_addr func, psx_addr param, int size, int arg);
int _exeque(void);
int _clr(psx_addr rect, uint32_t rgb);
int _dws(psx_addr rect, psx_addr p);
int _drs(psx_addr rect, psx_addr p);
void _ctl(uint32_t word);
int _cwb(psx_addr p, int n);
void _cwc(psx_addr p);
uint32_t _getctl(int n);
void _otc(psx_addr ot, int n);
uint32_t _param(int n);
int _reset(int mode);
uint32_t _status(void);
int _sync(int mode);

/* for the frame loop (libetc): the display, presented each VSync */
void psx_gpu_present(void);
void psx_gpu_exit(void); /* PSX_PORT_DUMP_VRAM=path: the VRAM as a PNG */
#endif
