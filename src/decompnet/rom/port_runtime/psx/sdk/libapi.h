/* Nothing of libapi is implemented yet: gen/psx_sdk_stubs.c logs each of its functions the game calls. */
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
#endif
