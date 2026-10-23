/* libetc: VSync paces the game to 60 Hz and is where the window is pumped and presented. */
#include <stdlib.h>
#include <time.h>

#include "../psx_runtime.h"
#include "libetc.h"
#include "libgpu.h"
#include "libpad.h"

static int frames;
static struct timespec next;

static void add_ns(struct timespec *t, long ns) {
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) { t->tv_nsec -= 1000000000L; t->tv_sec++; }
}

static void one_frame(void) {
    struct timespec now;
    uint8_t r, g, b;
    psx_gpu_clear_color(&r, &g, &b);
    host_frame(r, g, b);
    psx_pad_update();
    if (host_quit()) {
        psx_log("psx: window closed after %d frames\n", frames);
        exit(0);
    }
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (!next.tv_sec) next = now;
    add_ns(&next, 16683333L); /* NTSC: 59.94 Hz */
    if (now.tv_sec > next.tv_sec + 1) next = now; /* far behind (a debugger, a stall): don't race to catch up */
    else {
        struct timespec d;
        d.tv_sec = next.tv_sec - now.tv_sec;
        d.tv_nsec = next.tv_nsec - now.tv_nsec;
        if (d.tv_nsec < 0) { d.tv_nsec += 1000000000L; d.tv_sec--; }
        if (d.tv_sec >= 0) nanosleep(&d, NULL); /* macOS has no clock_nanosleep */
    }
    frames++;
}

#ifndef PSX_GAME_HAS_VSync /* the game has its own, as matched C */
int VSync(int mode) {
    static int logged;
    int i;
    if (!logged++) psx_log("etc: first VSync(%d)\n", mode);
    if (mode < 0) return frames;
    if (mode == 1) return 0; /* horizontal lines since the last vblank: none counted yet */
    for (i = 0; i < (mode > 1 ? mode : 1); i++) one_frame();
    return frames;
}
#endif
