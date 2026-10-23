/* Window and keyboard through SDL2. Arrows: d-pad; Z/X/A/S: cross/circle/square/triangle; Return: start;
 * right shift: select; Q/W: L1/R1; 1/2: L2/R2. */
#include <SDL.h>
#include <stdlib.h>

#include "psx_runtime.h"

static SDL_Window *win;
static SDL_Renderer *ren;
static SDL_Texture *tex;
static int tex_w, tex_h, quit;
static uint32_t pixels[1024 * 512];

const char *host_name(void) { return "SDL2"; }

int host_init(const char *title, int w, int h) {
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS) != 0) return 0;
    win = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h, SDL_WINDOW_RESIZABLE);
    if (!win) return 0;
    ren = SDL_CreateRenderer(win, -1, 0);
    SDL_SetHint(SDL_HINT_RENDER_SCALE_QUALITY, "0"); /* nearest-neighbour */
    host_present(NULL, 0, 0, 0, 0, 0);
    return 1;
}

void host_frame(void) {
    SDL_Event e;
    while (SDL_PollEvent(&e))
        if (e.type == SDL_QUIT) quit = 1;
}

void host_present(const uint16_t *vram, int x, int y, int w, int h, int rgb24) {
    if (!ren) return;
    SDL_SetRenderDrawColor(ren, 0, 0, 0, 255);
    SDL_RenderClear(ren);
    if (vram && w > 0 && h > 0) {
        if (!tex || tex_w != w || tex_h != h) {
            if (tex) SDL_DestroyTexture(tex);
            tex = SDL_CreateTexture(ren, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING, w, h);
            tex_w = w;
            tex_h = h;
        }
        if (tex) {
            gpu_display_rgba(vram, x, y, w, h, rgb24, pixels);
            SDL_UpdateTexture(tex, NULL, pixels, w * 4);
            SDL_RenderCopy(ren, tex, NULL, NULL); /* the whole window: a PS1 picture is 4:3 at any width */
        }
    }
    {   /* PSX_PORT_DUMP_SCREEN=path: what the window shows, read back every 30 frames */
        static int n;
        const char *dump = getenv("PSX_PORT_DUMP_SCREEN");
        int ww, wh;
        if (dump && *dump && n++ % 30 == 29 && SDL_GetRendererOutputSize(ren, &ww, &wh) == 0 && ww > 0 && wh > 0) {
            uint32_t *shot = (uint32_t *)SDL_malloc((size_t)ww * (size_t)wh * 4);
            if (shot && SDL_RenderReadPixels(ren, NULL, SDL_PIXELFORMAT_ARGB8888, shot, ww * 4) == 0)
                gpu_write_png(dump, shot, ww, wh);
            SDL_free(shot);
        }
    }
    SDL_RenderPresent(ren);
}

uint16_t host_pad(void) {
    static const struct { SDL_Scancode key; uint16_t bit; } map[] = {
        {SDL_SCANCODE_RSHIFT, 1 << 0}, {SDL_SCANCODE_RETURN, 1 << 3}, {SDL_SCANCODE_UP, 1 << 4},
        {SDL_SCANCODE_RIGHT, 1 << 5}, {SDL_SCANCODE_DOWN, 1 << 6}, {SDL_SCANCODE_LEFT, 1 << 7},
        {SDL_SCANCODE_1, 1 << 8}, {SDL_SCANCODE_2, 1 << 9}, {SDL_SCANCODE_Q, 1 << 10}, {SDL_SCANCODE_W, 1 << 11},
        {SDL_SCANCODE_S, 1 << 12}, {SDL_SCANCODE_X, 1 << 13}, {SDL_SCANCODE_Z, 1 << 14}, {SDL_SCANCODE_A, 1 << 15},
    };
    const Uint8 *keys = SDL_GetKeyboardState(NULL);
    uint16_t bits = 0;
    size_t i;
    if (!win) return 0;
    for (i = 0; i < sizeof map / sizeof map[0]; i++)
        if (keys[map[i].key]) bits |= map[i].bit;
    return bits;
}

int host_quit(void) { return quit; }

void host_shutdown(void) {
    if (win) SDL_DestroyWindow(win);
    SDL_Quit();
}
