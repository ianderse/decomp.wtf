#ifndef PSX_LIBGTE_H
#define PSX_LIBGTE_H
#include "psx_sdk_types.h"
void SetRotMatrix(MATRIX *m);
void SetTransMatrix(MATRIX *m);
MATRIX *RotMatrix(SVECTOR *r, MATRIX *m);
MATRIX *TransMatrix(MATRIX *m, VECTOR *v);
MATRIX *ScaleMatrix(MATRIX *m, VECTOR *v);
MATRIX *MulMatrix0(MATRIX *m0, MATRIX *m1, MATRIX *m2);
VECTOR *ApplyMatrix(MATRIX *m, SVECTOR *v0, VECTOR *v1);
int32_t SquareRoot0(int32_t a);
void InitGeom(void);
void SetGeomOffset(int ofx, int ofy);
void SetGeomScreen(int h);
int rsin(int a);
int rcos(int a);
#endif
