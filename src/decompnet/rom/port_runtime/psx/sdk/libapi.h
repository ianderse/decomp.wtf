/* libapi: the BIOS's C library calls, the heap, events and critical sections (libapi.c); the rest are logging stubs
 * in gen/psx_sdk_stubs.c. */
#ifndef PSX_LIBAPI_H
#define PSX_LIBAPI_H
#include "psx_sdk_types.h"
/* the BIOS's C library calls (the A0 table), behind the game's own stub names in gen/psx_sdk_stubs.c */
void *psx_bios_memcpy(void *dst, const void *src, int n);
void *psx_bios_memset(void *dst, int c, int n);
void *psx_bios_memmove(void *dst, const void *src, int n);
void *psx_bios_memchr(const void *s, int c, int n);
int psx_bios_strlen(const char *s);
char *psx_bios_strcpy(char *d, const char *s);
char *psx_bios_strncpy(char *d, const char *s, int n);
char *psx_bios_strcat(char *d, const char *s);
int psx_bios_rand(void);
void psx_bios_srand(unsigned seed);
void psx_bios_InitHeap(void *addr, unsigned size);
void *psx_bios_malloc(unsigned n);
void psx_bios_free(void *p);
void *psx_bios_calloc(unsigned n, unsigned size);
void *psx_bios_realloc(void *p, unsigned n);
int psx_bios_OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler);
int psx_bios_CloseEvent(unsigned event);
int psx_bios_EnableEvent(unsigned event);
int psx_bios_DisableEvent(unsigned event);
int psx_EnterCriticalSection(void);
int psx_ExitCriticalSection(void);
int OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler);
int CloseEvent(unsigned event);
int EnableEvent(unsigned event);
int DisableEvent(unsigned event);
int EnterCriticalSection(void);
int ExitCriticalSection(void);
#endif
