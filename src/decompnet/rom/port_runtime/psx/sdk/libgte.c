/* libgte's matrix helpers that are pure arithmetic, from PSY-Q's documented behaviour (fixed point, 4096 = 1.0 and
 * 4096 = 360 degrees); the GTE's registers are set through psx/gte.c. Not compared with Sony's output yet: Sony's sine
 * table and rounding may differ in the last bit. */
#include <math.h>

#include "../psx_runtime.h"
#include "libgte.h"

int psx_gte_mfc2(int reg);
void psx_gte_ctc2(int reg, int value);

static int32_t pack(int16_t lo, int16_t hi) { return (int32_t)(((uint32_t)(uint16_t)hi << 16) | (uint16_t)lo); }

#ifndef PSX_GAME_HAS_SetRotMatrix /* the game has its own, as matched C */
void SetRotMatrix(MATRIX *m) {
    m = PSX_PTR(m);
    psx_gte_ctc2(0, pack(m->m[0][0], m->m[0][1]));
    psx_gte_ctc2(1, pack(m->m[0][2], m->m[1][0]));
    psx_gte_ctc2(2, pack(m->m[1][1], m->m[1][2]));
    psx_gte_ctc2(3, pack(m->m[2][0], m->m[2][1]));
    psx_gte_ctc2(4, m->m[2][2]);
}
#endif

#ifndef PSX_GAME_HAS_SetTransMatrix /* the game has its own, as matched C */
void SetTransMatrix(MATRIX *m) {
    m = PSX_PTR(m);
    psx_gte_ctc2(5, m->t[0]);
    psx_gte_ctc2(6, m->t[1]);
    psx_gte_ctc2(7, m->t[2]);
}
#endif

int rsin(int a) { return (int)lround(sin((a & 4095) * (2.0 * M_PI / 4096.0)) * 4096.0); }
int rcos(int a) { return (int)lround(cos((a & 4095) * (2.0 * M_PI / 4096.0)) * 4096.0); }

static void mul3(const int32_t a[3][3], const int32_t b[3][3], int32_t out[3][3]) {
    int i, j;
    for (i = 0; i < 3; i++)
        for (j = 0; j < 3; j++)
            out[i][j] = (int32_t)(((int64_t)a[i][0] * b[0][j] + (int64_t)a[i][1] * b[1][j] + (int64_t)a[i][2] * b[2][j]) >> 12);
}

/* PSY-Q: rotation about X, then Y, then Z, combined as M = Rx * Ry * Rz */
#ifndef PSX_GAME_HAS_RotMatrix /* the game has its own, as matched C */
MATRIX *RotMatrix(SVECTOR *r_in, MATRIX *m_in) {
    SVECTOR *r = PSX_PTR(r_in);
    MATRIX *m = PSX_PTR(m_in);
    int32_t sx = rsin(r->vx), cx = rcos(r->vx), sy = rsin(r->vy), cy = rcos(r->vy), sz = rsin(r->vz), cz = rcos(r->vz);
    int32_t rx[3][3] = {{4096, 0, 0}, {0, 0, 0}, {0, 0, 0}}, ry[3][3] = {{0}}, rz[3][3] = {{0}}, t[3][3], o[3][3];
    int i, j;
    rx[1][1] = cx; rx[1][2] = -sx; rx[2][1] = sx; rx[2][2] = cx;
    ry[0][0] = cy; ry[0][2] = sy; ry[1][1] = 4096; ry[2][0] = -sy; ry[2][2] = cy;
    rz[0][0] = cz; rz[0][1] = -sz; rz[1][0] = sz; rz[1][1] = cz; rz[2][2] = 4096;
    mul3(rx, ry, t);
    mul3(t, rz, o);
    for (i = 0; i < 3; i++)
        for (j = 0; j < 3; j++) m->m[i][j] = (int16_t)o[i][j];
    return m;
}
#endif

#ifndef PSX_GAME_HAS_TransMatrix /* the game has its own, as matched C */
MATRIX *TransMatrix(MATRIX *m, VECTOR *v) {
    m = PSX_PTR(m);
    v = PSX_PTR(v);
    m->t[0] = v->vx; m->t[1] = v->vy; m->t[2] = v->vz;
    return m;
}
#endif

#ifndef PSX_GAME_HAS_ScaleMatrix /* the game has its own, as matched C */
MATRIX *ScaleMatrix(MATRIX *m, VECTOR *v) { /* column j scaled by v[j] */
    int i;
    m = PSX_PTR(m);
    v = PSX_PTR(v);
    for (i = 0; i < 3; i++) {
        m->m[i][0] = (int16_t)(((int64_t)m->m[i][0] * v->vx) >> 12);
        m->m[i][1] = (int16_t)(((int64_t)m->m[i][1] * v->vy) >> 12);
        m->m[i][2] = (int16_t)(((int64_t)m->m[i][2] * v->vz) >> 12);
    }
    return m;
}
#endif

#ifndef PSX_GAME_HAS_MulMatrix0 /* the game has its own, as matched C */
MATRIX *MulMatrix0(MATRIX *m0, MATRIX *m1, MATRIX *m2) {
    int32_t a[3][3], b[3][3], o[3][3];
    int i, j;
    m0 = PSX_PTR(m0);
    m1 = PSX_PTR(m1);
    m2 = PSX_PTR(m2);
    for (i = 0; i < 3; i++)
        for (j = 0; j < 3; j++) { a[i][j] = m0->m[i][j]; b[i][j] = m1->m[i][j]; }
    mul3(a, b, o);
    for (i = 0; i < 3; i++)
        for (j = 0; j < 3; j++) m2->m[i][j] = (int16_t)o[i][j];
    return m2;
}
#endif

#ifndef PSX_GAME_HAS_ApplyMatrix /* the game has its own, as matched C */
VECTOR *ApplyMatrix(MATRIX *m, SVECTOR *v0, VECTOR *v1) {
    int32_t in[3], out[3];
    int i;
    m = PSX_PTR(m);
    v0 = PSX_PTR(v0);
    v1 = PSX_PTR(v1);
    in[0] = v0->vx; in[1] = v0->vy; in[2] = v0->vz;
    for (i = 0; i < 3; i++)
        out[i] = (int32_t)(((int64_t)m->m[i][0] * in[0] + (int64_t)m->m[i][1] * in[1] + (int64_t)m->m[i][2] * in[2]) >> 12);
    v1->vx = out[0]; v1->vy = out[1]; v1->vz = out[2];
    return v1;
}
#endif

#ifndef PSX_GAME_HAS_SquareRoot0 /* the game has its own, as matched C */
int32_t SquareRoot0(int32_t a) {
    uint32_t x = (uint32_t)a, r = 0, bit = 1u << 30;
    if (a <= 0) return 0;
    while (bit > x) bit >>= 2;
    while (bit) {
        if (x >= r + bit) { x -= r + bit; r = (r >> 1) + bit; }
        else r >>= 1;
        bit >>= 2;
    }
    return (int32_t)r;
}
#endif
