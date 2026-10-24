#ifndef PSX_LIBPAD_H
#define PSX_LIBPAD_H
#include "psx_sdk_types.h"
void PadInitDirect(uint8_t *pad1, uint8_t *pad2);
int PadStartCom(void);
void PadStopCom(void);
int PadGetState(int port);
int PadInfoMode(int port, int term, int offs);
int PadSetMainMode(int port, int offs, int lock);
void PadSetAct(int port, uint8_t *data, int len);
int PadSetActAlign(int port, uint8_t *data);
void psx_pad_update(void); /* each VSync: the keyboard and the pad script into the buffers PadInitDirect was given */
#endif
