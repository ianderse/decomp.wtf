/* libapi: the BIOS's C library calls, the heap, events (opened, delivered, tested, waited for) and critical sections
 * (libapi.c); the rest are logging stubs in gen/psx_sdk_stubs.c. */
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
/* events: the classes and specs the card driver delivers (psx-spx "BIOS Events"; PSY-Q's kernel.h names) */
#define PSX_EV_HwCARD 0xF0000011u
#define PSX_EV_SwCARD 0xF4000001u
#define PSX_EvSpIOE 0x0004u
#define PSX_EvSpTIMOUT 0x0100u
#define PSX_EvSpNEW 0x2000u
#define PSX_EvSpERROR 0x8000u
#define PSX_EvMdINTR 0x1000u
#define PSX_EvMdNOINTR 0x2000u
int psx_bios_OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler);
int psx_bios_CloseEvent(unsigned event);
int psx_bios_EnableEvent(unsigned event);
int psx_bios_DisableEvent(unsigned event);
int psx_bios_DeliverEvent(unsigned cls, unsigned spec);
int psx_bios_UnDeliverEvent(unsigned cls, unsigned spec);
int psx_bios_TestEvent(unsigned event);
int psx_bios_WaitEvent(unsigned event);
int psx_EnterCriticalSection(void);
int psx_ExitCriticalSection(void);
int OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler);
int CloseEvent(unsigned event);
int EnableEvent(unsigned event);
int DisableEvent(unsigned event);
int DeliverEvent(unsigned cls, unsigned spec);
int UnDeliverEvent(unsigned cls, unsigned spec);
int TestEvent(unsigned event);
int WaitEvent(unsigned event);
int EnterCriticalSection(void);
int ExitCriticalSection(void);
#endif
