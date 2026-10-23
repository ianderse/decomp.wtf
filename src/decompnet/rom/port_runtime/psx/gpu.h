/* A software PS1 GPU: 1024x512 16-bit VRAM, the GP0 drawing commands and the GP1 display controls, rasterised the
 * way psx-spx's GPU chapter describes (https://psx-spx.consoledev.net/graphicsprocessingunitgpu/). Self-contained
 * (no runtime headers), so a test can compile it on its own. */
#ifndef PSX_GPU_H
#define PSX_GPU_H
#include <stdint.h>

#define GPU_VRAM_W 1024
#define GPU_VRAM_H 512
extern uint16_t gpu_vram[GPU_VRAM_W * GPU_VRAM_H];

typedef struct {
    int x, y, w, h;   /* the display area in VRAM */
    int rgb24;        /* 24-bit colour (MDEC movies) */
    int enabled;      /* GP1(03) */
} GpuDisplay;

typedef struct {
    unsigned long packets, primitives, pixels, fills, loads;
} GpuStats;
extern GpuStats gpu_stats;

void gpu_reset(void);
/* a packet of GP0 words (commands with their parameters, as one node of a DMA chain holds); returns the words used */
int gpu_gp0(const uint32_t *words, int n);
void gpu_gp1(uint32_t word);
uint32_t gpu_read_stat(void);
uint32_t gpu_info(uint32_t which); /* GP1(10h) answers */
uint32_t gpu_ctl(int cmd);         /* the last GP1 word written with that command (0 if never) */

/* the linked list a DrawOTag/DMA2 chain walks: a node is a word (next address in bits 0-23, number of words after it
 * in 24-31) and then its words; it ends at next == 0xFFFFFF. ram is the PS1's 2 MB of RAM (addresses masked to it).
 * Returns the nodes walked. */
long gpu_draw_chain(const uint8_t *ram, uint32_t start);

/* VRAM transfers (LoadImage / StoreImage / MoveImage / ClearImage) */
void gpu_load_image(int x, int y, int w, int h, const uint16_t *src);
void gpu_store_image(int x, int y, int w, int h, uint16_t *dst);
void gpu_move_image(int sx, int sy, int dx, int dy, int w, int h);
void gpu_fill(int x, int y, int w, int h, uint32_t rgb24);

void gpu_get_display(GpuDisplay *d);
void gpu_set_display(int x, int y, int w, int h, int rgb24); /* PutDispEnv's rectangle (GP1 05h/08h with its size) */
/* the display area as 0xFFRRGGBB pixels (w*h of them), for a window to present */
void gpu_display_rgba(const uint16_t *vram, int x, int y, int w, int h, int rgb24, uint32_t *out);
/* the whole VRAM as an uncompressed PNG; 0 on success */
int gpu_dump_vram_png(const char *path);
/* 0xFFRRGGBB pixels as an uncompressed PNG; 0 on success */
int gpu_write_png(const char *path, const uint32_t *px, int w, int h);
#endif
