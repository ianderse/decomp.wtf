/* No window: for machines without a usable SDL2 or Cocoa, and for PSX_HEADLESS runs. */
#include "psx_runtime.h"

const char *host_name(void) { return "headless build"; }
int host_init(const char *title, int w, int h) { (void)title; (void)w; (void)h; return 0; }
void host_frame(void) {}
void host_present(const uint16_t *vram, int x, int y, int w, int h, int rgb24) {
    (void)vram; (void)x; (void)y; (void)w; (void)h; (void)rgb24;
}
uint16_t host_pad(void) { return 0; }
int host_quit(void) { return 0; }
void host_shutdown(void) {}
