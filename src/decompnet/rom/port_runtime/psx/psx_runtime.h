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

/* ---- the memory convention: PS1 address A lives at host address PSX_HOST_BASE + A.
 * PS1 RAM (0x80000000) is mapped at 0x4_8000_0000, the scratchpad (0x1F800000) at 0x4_1F80_0000, the game's stack
 * in the same 4 GB window (0x4_6000_0000: the game sees sp as 0x6000xxxx), the game's string literals that it holds
 * as pointers at 0x4_7000_0000 (PSX_LITERAL_BASE), and every data symbol is the absolute symbol PSX_HOST_BASE + its
 * PS1 address (gen/psx_symbols.S). The window is clear of everything macOS maps: the executable (0x1_0000_0000 plus
 * a slide), the dyld shared cache (0x1_8000_0000 up to about 0x2_2B00_0000), the host's stacks and heap. Nothing at
 * the window's base is mapped, so a null PS1 pointer faults. The game's own code is translated to this by
 * tools/cc_ptr32.py: every access through a 32-bit pointer goes to zext(pointer) + PSX_HOST_BASE, and a host
 * pointer handed to the game is its low 32 bits.
 *
 * The rule for the runtime: a pointer the game gives you is a PS1 address (a 32-bit value in a 64-bit register whose
 * upper half the ABI leaves undefined): turn it into a host pointer with psx_host() (or PSX_PTR() on a pointer-typed
 * parameter). A pointer you give the game (a return value, a field you fill in) must point into the window: RAM,
 * the game's stack, or psx_malloc's heap in RAM; never host memory from malloc or a host global. A function
 * pointer the game gives you is a PS1 address too: call it through psx_host_fn(). A host function is never handed
 * to the game as a pointer: one that must be (a callback the runtime registers) needs an entry of its own in
 * psx_functions with a PS1 address. PSX_HOST_BASE must agree with tools/cc_ptr32.py and export_port.py. */
#define PSX_HOST_BASE 0x400000000ull
#define PSX_RAM_BASE 0x80000000u     /* PS1 addresses */
#define PSX_RAM_SIZE 0x00200000u
#define PSX_SCRATCH_BASE 0x1F800000u
#define PSX_SCRATCH_SIZE 0x00010000u /* scratchpad and the I/O ports after it, as plain memory */
#define PSX_STACK_TOP 0x60800000u    /* the game's stack: 8 MB below this (a fallback lower in the window if taken) */
#define PSX_STACK_SIZE 0x00800000u
#define PSX_LITERAL_BASE 0x70000000u /* the game's string literals (tools/cc_ptr32.py --collect-literals) */
#define PSX_LITERAL_SIZE 0x00100000u
extern const unsigned char psx_literal_image[];
extern const uint32_t psx_literal_size;

/* PS1 address -> host pointer (0 stays NULL, so the runtime's null checks still work) */
static inline void *psx_host(uint32_t addr) {
    return addr ? (void *)(uintptr_t)(PSX_HOST_BASE + addr) : (void *)0;
}
/* host pointer into the window -> PS1 address */
static inline uint32_t psx_ps1(const void *p) { return (uint32_t)(uintptr_t)p; }
/* a pointer-typed parameter the game passed: only its low 32 bits mean anything */
#define PSX_PTR(p) psx_host((uint32_t)(uintptr_t)(p))
/* is a host address inside the window? */
static inline int psx_in_window(const void *p) {
    return (uintptr_t)p >= PSX_HOST_BASE && (uintptr_t)p - PSX_HOST_BASE < 0x100000000ull;
}

/* a PS1 function address (a function pointer from the game's data or code) -> the host function to call: the
 * table's function at that address (overlays told apart by their code in RAM); anything else ends the run with the
 * PS1 address. Calls through the game's function pointers go here (tools/cc_ptr32.py). */
void *psx_host_fn(uint32_t addr);

/* the PS1 heap in RAM (InitHeap's region, as the BIOS's malloc): memory the runtime may hand the game */
void psx_heap_init(uint32_t addr, uint32_t size);
void *psx_malloc(uint32_t size);
void psx_free(void *p);

void psx_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void psx_ram_init(void);
void *psx_map(uint32_t ps1_addr, uint32_t size); /* a part of the window, or NULL if it is taken */
void psx_dispatch_init(void);
int psx_unmatched(const char *name);
int psx_sdk_stub(const char *name, int *calls);
int psx_soft(void);
void psx_exit_report(void);
void psx_gpu_exit(void); /* psx/sdk/libgpu.c: the GPU's counts, and PSX_PORT_DUMP_VRAM */
void psx_gpu_selftest(int frames); /* PSX_GPU_SELFTEST=N: N frames drawn through libgpu before the game's main */

/* host window and input (psx/host_*.c) */
int host_init(const char *title, int w, int h);
void host_frame(void); /* pump events */
/* show the display area of VRAM (1024x512 halfwords; rgb24: 24-bit pixels packed across them), scaled to the window
 * with nearest-neighbour; vram NULL: black (display off) */
void host_present(const uint16_t *vram, int x, int y, int w, int h, int rgb24);
/* the display area as 0xFFRRGGBB pixels (psx/gpu.c) */
void gpu_display_rgba(const uint16_t *vram, int x, int y, int w, int h, int rgb24, uint32_t *out);
int gpu_write_png(const char *path, const uint32_t *px, int w, int h);
uint16_t host_pad(void);                         /* PS1 pad bits, 1 = pressed (PSY-Q PAD* order) */
int host_quit(void);
void host_shutdown(void);
const char *host_name(void);

/* call fn on a stack of our own (psx/stack.S): the game's, in the window, so its locals' addresses are PS1 addresses */
void psx_call_on_stack(void (*fn)(void), void *top);
#endif
