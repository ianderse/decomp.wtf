/* No window: for machines without a usable SDL2 or Cocoa, and for PSX_HEADLESS runs. */
#include "psx_runtime.h"

const char *host_name(void) { return "headless build"; }
int host_init(const char *title, int w, int h) { (void)title; (void)w; (void)h; return 0; }
void host_frame(uint8_t r, uint8_t g, uint8_t b) { (void)r; (void)g; (void)b; }
uint16_t host_pad(void) { return 0; }
int host_quit(void) { return 0; }
void host_shutdown(void) {}
