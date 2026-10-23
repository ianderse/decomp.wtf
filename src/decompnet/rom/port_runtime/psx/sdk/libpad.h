#ifndef PSX_LIBPAD_H
#define PSX_LIBPAD_H
#include "psx_sdk_types.h"
void PadInitDirect(uint8_t *pad1, uint8_t *pad2);
int PadStartCom(void);
void PadStopCom(void);
void psx_pad_update(void); /* each VSync: the keyboard into the buffers PadInitDirect was given */
#endif
