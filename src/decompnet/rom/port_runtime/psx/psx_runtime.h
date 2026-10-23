/* The port's runtime: what psx/ and gen/ share. Not included by the game's own files (they see common.h only). */
#ifndef PSX_RUNTIME_H
#define PSX_RUNTIME_H
#include <stddef.h>
#include <stdint.h>

/* generated per game (gen/psx_config.c) */
typedef struct {
    const char *title;
    const char *image;          /* the PS-X EXE, relative to the source tree */
    const char *source_dir;     /* the exported tree (absolute, at generation time) */
    uint32_t load_addr, load_size, file_offset;
    uint32_t bss_start, bss_end; /* from the start-up code, or image end .. 0x80200000 */
    uint32_t initial_pc, initial_sp, gp;
    const char *main_name;
    void (*main_fn)(void);
} PsxConfig;
extern const PsxConfig psx_config;

/* every function at its PS1 address (gen/psx_functions.c): indirect calls through the game's data land here */
typedef struct {
    uint32_t vram;
    uint32_t word0, word1;      /* its first two instructions, to tell overlays at the same address apart */
    void (*fn)(void);
    const char *name;
    const char *segment;
    uint8_t kind;               /* 0 matched C, 1 unmatched stub, 2 Sony stub, 3 Sony implemented */
} PsxFunction;
extern const PsxFunction psx_functions[];
extern const size_t psx_function_count;
const PsxFunction *psx_function_at(uint32_t vram);
const char *psx_host_name(const void *fn);

/* a pointer from the game's code: a __ptr32 value passed in a 64-bit register, whose upper half the ABI leaves
 * undefined. Everything the game points at is below 4 GB, so the low half is the pointer. */
#define PSX_PTR(p) ((void *)(uintptr_t)(uint32_t)(uintptr_t)(p))

#define PSX_RAM_BASE 0x80000000u
#define PSX_RAM_SIZE 0x00200000u
#define PSX_SCRATCH_BASE 0x1F800000u
#define PSX_SCRATCH_SIZE 0x00010000u /* scratchpad and the I/O ports after it, as plain memory */

void psx_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void psx_ram_init(void);
void psx_dispatch_init(void);
int psx_unmatched(const char *name);
int psx_sdk_stub(const char *name, int *calls);
int psx_soft(void);
void psx_exit_report(void);

/* host window and input (psx/host_*.c) */
int host_init(const char *title, int w, int h);
void host_frame(uint8_t r, uint8_t g, uint8_t b); /* pump events, clear, present */
uint16_t host_pad(void);                         /* PS1 pad bits, 1 = pressed (PSY-Q PAD* order) */
int host_quit(void);
void host_shutdown(void);
const char *host_name(void);

/* call fn on a stack of our own below 4 GB (psx/stack.S), so addresses of the game's locals fit in 32 bits */
void psx_call_on_stack(void (*fn)(void), void *top);
#endif
