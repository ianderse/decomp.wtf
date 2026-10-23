/* Window and keyboard through SDL2. Arrows: d-pad; Z/X/A/S: cross/circle/square/triangle; Return: start;
 * right shift: select; Q/W: L1/R1; 1/2: L2/R2. */
#include <SDL.h>

#include "psx_runtime.h"

static SDL_Window *win;
static SDL_Renderer *ren;
static int quit;

const char *host_name(void) { return "SDL2"; }

int host_init(const char *title, int w, int h) {
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS) != 0) return 0;
    win = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h, 0);
    if (!win) return 0;
    ren = SDL_CreateRenderer(win, -1, 0);
    host_frame(0, 0, 0);
    return 1;
}

void host_frame(uint8_t r, uint8_t g, uint8_t b) {
    SDL_Event e;
    while (SDL_PollEvent(&e))
        if (e.type == SDL_QUIT) quit = 1;
    if (ren) {
        SDL_SetRenderDrawColor(ren, r, g, b, 255);
        SDL_RenderClear(ren);
        SDL_RenderPresent(ren);
    }
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
