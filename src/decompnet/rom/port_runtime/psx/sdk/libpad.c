/* libpad: PadInitDirect's buffers get the keyboard as a digital pad (id 0x41) on port 1, nothing on port 2. */
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
    psx_log("pad: PadInitDirect(%p, %p)\n", (void *)pad1, (void *)pad2);
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
    uint16_t bits = host_pad();
    if (buf1) {
        buf1[0] = 0x00; /* connected */
        buf1[1] = 0x41; /* digital pad, 1 halfword of buttons */
        buf1[2] = (uint8_t)~(bits & 0xFF); /* active low */
        buf1[3] = (uint8_t)~(bits >> 8);
    }
    if (buf2) buf2[0] = 0xFF; /* no controller */
    (void)started;
}
