/* libgpu: the environment calls are recorded, nothing is drawn yet (the next pass draws the ordering tables). */
#include <string.h>

#include "../psx_runtime.h"
#include "libgpu.h"

static DRAWENV draw_env;
static DISPENV disp_env;
static int have_draw, display_on, debug_level;

#ifndef PSX_GAME_HAS_ResetGraph /* the game has its own, as matched C */
int ResetGraph(int mode) {
    psx_log("gpu: ResetGraph(%d)\n", mode);
    return 0;
}
#endif

#ifndef PSX_GAME_HAS_SetGraphDebug /* the game has its own, as matched C */
int SetGraphDebug(int level) {
    int old = debug_level;
    debug_level = level;
    return old;
}
#endif

#ifndef PSX_GAME_HAS_SetDispMask /* the game has its own, as matched C */
void SetDispMask(int mask) { display_on = mask; }
#endif

#ifndef PSX_GAME_HAS_SetDefDrawEnv /* the game has its own, as matched C */
DRAWENV *SetDefDrawEnv(DRAWENV *env, int x, int y, int w, int h) {
    env = PSX_PTR(env);
    memset(env, 0, sizeof *env);
    env->clip.x = (int16_t)x; env->clip.y = (int16_t)y; env->clip.w = (int16_t)w; env->clip.h = (int16_t)h;
    env->ofs[0] = (int16_t)x; env->ofs[1] = (int16_t)y;
    env->tpage = 10; /* GetTPage(0, 0, 640, 0) */
    env->dtd = 1;
    env->dfe = 1;
    return env;
}
#endif

#ifndef PSX_GAME_HAS_SetDefDispEnv /* the game has its own, as matched C */
DISPENV *SetDefDispEnv(DISPENV *env, int x, int y, int w, int h) {
    env = PSX_PTR(env);
    memset(env, 0, sizeof *env);
    env->disp.x = (int16_t)x; env->disp.y = (int16_t)y; env->disp.w = (int16_t)w; env->disp.h = (int16_t)h;
    env->screen.w = 256; env->screen.h = 240;
    return env;
}
#endif

#ifndef PSX_GAME_HAS_PutDrawEnv /* the game has its own, as matched C */
DRAWENV *PutDrawEnv(DRAWENV *env) {
    static int logged;
    env = PSX_PTR(env);
    draw_env = *env;
    have_draw = 1;
    if (!logged++)
        psx_log("gpu: PutDrawEnv clip %d,%d %dx%d, clear %s to %d,%d,%d\n", env->clip.x, env->clip.y, env->clip.w,
                env->clip.h, env->isbg ? "on" : "off", env->r0, env->g0, env->b0);
    return env;
}
#endif

#ifndef PSX_GAME_HAS_PutDispEnv /* the game has its own, as matched C */
DISPENV *PutDispEnv(DISPENV *env) {
    static int logged;
    env = PSX_PTR(env);
    disp_env = *env;
    if (!logged++)
        psx_log("gpu: PutDispEnv display %d,%d %dx%d\n", env->disp.x, env->disp.y, env->disp.w, env->disp.h);
    return env;
}
#endif

void psx_gpu_clear_color(uint8_t *r, uint8_t *g, uint8_t *b) {
    if (have_draw && draw_env.isbg) { *r = draw_env.r0; *g = draw_env.g0; *b = draw_env.b0; }
    else { *r = 16; *g = 16; *b = 24; }
}
