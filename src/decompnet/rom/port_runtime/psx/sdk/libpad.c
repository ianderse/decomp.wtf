/* libpad: PadInitDirect's buffers get the keyboard and PSX_PAD_SCRIPT's presses (psx_pad_bits, psx/pad_script.c)
 * as a digital pad (id 0x41) on port 1, nothing on port 2. */
#include <stddef.h>

#include "../psx_runtime.h"
#include "libpad.h"

static uint8_t *buf1, *buf2;
static int started;

#ifndef PSX_GAME_HAS_PadInitDirect /* the game has its own, as matched C */
void PadInitDirect(uint8_t *pad1, uint8_t *pad2) {
    pad1 = PSX_PTR(pad1);
    pad2 = PSX_PTR(pad2);
    buf1 = pad1;
    buf2 = pad2;
    psx_log("pad: PadInitDirect(0x%08X, 0x%08X)\n", psx_ps1(pad1), psx_ps1(pad2));
    psx_pad_update();
}
#endif

#ifndef PSX_GAME_HAS_PadStartCom /* the game has its own, as matched C */
int PadStartCom(void) {
    started = 1;
    return 1;
}
#endif

#ifndef PSX_GAME_HAS_PadStopCom /* the game has its own, as matched C */
void PadStopCom(void) { started = 0; }
#endif

void psx_pad_update(void) {
    uint16_t bits = psx_pad_bits();
    if (buf1) {
        buf1[0] = 0x00; /* connected */
        buf1[1] = 0x41; /* digital pad, 1 halfword of buttons */
        buf1[2] = (uint8_t)~(bits & 0xFF); /* active low */
        buf1[3] = (uint8_t)~(bits >> 8);
    }
    if (buf2) buf2[0] = 0xFF; /* no controller */
    (void)started;
}

/* libpad's state and actuator calls, for a game that runs libpad's state machine itself (SLUS-01252's pad reader):
 * port 0x00 is a digital pad once PadInitDirect has its buffers, PadStateFindCTP1 (2: found, no actuators to set
 * up), port 0x10 nothing (PadStateDiscon, 0); no modes, no motors, nothing to align */
#ifndef PSX_GAME_HAS_PadGetState
int PadGetState(int port) { return (port & 0xFF) == 0x00 && buf1 ? 2 : 0; }
#endif
#ifndef PSX_GAME_HAS_PadInfoMode
int PadInfoMode(int port, int term, int offs) {
    (void)port;
    (void)term;
    (void)offs;
    return 0;
}
#endif
#ifndef PSX_GAME_HAS_PadSetMainMode
int PadSetMainMode(int port, int offs, int lock) {
    (void)port;
    (void)offs;
    (void)lock;
    return 0;
}
#endif
#ifndef PSX_GAME_HAS_PadSetAct
void PadSetAct(int port, uint8_t *data, int len) {
    (void)port;
    (void)data;
    (void)len;
}
#endif
#ifndef PSX_GAME_HAS_PadSetActAlign
int PadSetActAlign(int port, uint8_t *data) {
    (void)port;
    (void)data;
    return 0;
}
#endif
