/* The BIOS's C library calls (A0 table) the game reaches through its own stubs: plain C library behaviour. */
#include <stdlib.h>
#include <string.h>

#include "libapi.h"

void *psx_bios_memcpy(void *dst, const void *src, int n) { return n > 0 ? memcpy(dst, src, (size_t)n) : dst; }
void *psx_bios_memset(void *dst, int c, int n) { return n > 0 ? memset(dst, c, (size_t)n) : dst; }
void *psx_bios_memmove(void *dst, const void *src, int n) { return n > 0 ? memmove(dst, src, (size_t)n) : dst; }
void *psx_bios_memchr(const void *s, int c, int n) { return n > 0 ? memchr(s, c, (size_t)n) : NULL; }
int psx_bios_strlen(const char *s) { return s ? (int)strlen(s) : 0; }
char *psx_bios_strcpy(char *d, const char *s) { return strcpy(d, s); }
char *psx_bios_strncpy(char *d, const char *s, int n) { return strncpy(d, s, (size_t)n); }
char *psx_bios_strcat(char *d, const char *s) { return strcat(d, s); }

/* the BIOS's rand: the classic LCG, 15 bits */
static unsigned rand_state = 0x24040001;
int psx_bios_rand(void) {
    rand_state = rand_state * 1103515245u + 12345u;
    return (int)((rand_state >> 16) & 0x7FFF);
}
void psx_bios_srand(unsigned seed) { rand_state = seed; }
