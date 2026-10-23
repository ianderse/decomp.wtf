/* PS1 RAM in the window: PS1 address A at host PSX_HOST_BASE + A (psx_runtime.h), so 2 MB at 0x1_8000_0000 and the
 * scratchpad at 0x1_1F80_0000. Raw addresses, 32-bit pointers stored in the game's data and overlay images then work
 * unchanged once tools/cc_ptr32.py has translated the game's accesses. Also the PS1 heap (InitHeap's region). */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include "psx_runtime.h"

#if defined(__APPLE__)
#include <mach/mach.h>
#include <mach/mach_vm.h>
/* is everything in [lo, hi) unmapped or a no-access reservation? macOS reserves the shared region from 0x1_8000_0000
 * and slides the dyld shared cache into it at every boot; the part before the cache is such a reservation (measured
 * 2026-10-23: 0x1_8000_0000-0x1_8795_8000 at protection 0, the cache's text from 0x1_879E_0000) */
static int only_reserved(uint64_t lo, uint64_t hi) {
    mach_vm_address_t at = lo;
    while (at < hi) {
        mach_vm_address_t addr = at;
        mach_vm_size_t size = 0;
        natural_t depth = 64;
        vm_region_submap_info_data_64_t info;
        mach_msg_type_number_t count = VM_REGION_SUBMAP_INFO_COUNT_64;
        if (mach_vm_region_recurse(mach_task_self(), &addr, &size, &depth, (vm_region_recurse_info_t)&info,
                                   &count) != KERN_SUCCESS)
            return 1; /* nothing mapped from here up */
        if (addr >= hi) return 1;
        if (info.protection != VM_PROT_NONE) return 0;
        at = addr + size;
    }
    return 1;
}
#endif

/* map [addr, addr+size) of the window (PS1 addresses), or NULL if something live is there. MAP_FIXED replaces
 * whatever it covers, so it is used only where nothing accessible is mapped: the executable, the host's own stack
 * and the dyld shared cache all move at every launch or boot. */
void *psx_map(uint32_t addr, uint32_t size) {
    void *want = (void *)(uintptr_t)(PSX_HOST_BASE + addr);
    void *p;
#if defined(__APPLE__)
    if (!only_reserved((uint64_t)(uintptr_t)want, (uint64_t)(uintptr_t)want + size)) return NULL;
    p = mmap(want, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0);
#elif defined(MAP_FIXED_NOREPLACE)
    p = mmap(want, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON | MAP_FIXED_NOREPLACE, -1, 0);
#else
    p = mmap(want, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
#endif
    if (p == MAP_FAILED) return NULL;
    if (p != want) {
        munmap(p, size);
        return NULL;
    }
    return p;
}

static void map_required(uint32_t addr, uint32_t size, const char *what) {
    errno = 0;
    if (!psx_map(addr, size)) {
        fprintf(stderr, "psx: can't map %s at host 0x%llX (PS1 0x%08X, 0x%X bytes): something live is there (%s). "
                        "The dyld shared cache moves at every boot and the executable at every launch.\n",
                what, (unsigned long long)(PSX_HOST_BASE + addr), addr, size, errno ? strerror(errno) : "in use");
        exit(70);
    }
}

static unsigned char *read_file(const char *path, size_t *size) {
    FILE *f = fopen(path, "rb");
    unsigned char *buf;
    long n;
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    n = ftell(f);
    fseek(f, 0, SEEK_SET);
    buf = malloc((size_t)n);
    if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); buf = NULL; }
    fclose(f);
    *size = (size_t)n;
    return buf;
}

void psx_ram_init(void) {
    char path[4096];
    const char *dir = getenv("PSX_SOURCE_DIR");
    unsigned char *exe;
    size_t size;
    uint32_t load, len, off;
    map_required(PSX_RAM_BASE, PSX_RAM_SIZE, "PS1 RAM");
    map_required(PSX_SCRATCH_BASE, PSX_SCRATCH_SIZE, "the scratchpad");
    snprintf(path, sizeof path, "%s/%s", dir ? dir : psx_config.source_dir, psx_config.image);
    exe = read_file(path, &size);
    if (!exe || size < 0x800 || memcmp(exe, "PS-X EXE", 8) != 0) {
        fprintf(stderr, "psx: can't read the game's executable %s (set PSX_SOURCE_DIR to the exported tree)\n", path);
        exit(66);
    }
    /* the header gives where it loads and how much; the generator's copy is the fallback */
    memcpy(&load, exe + 0x18, 4);
    memcpy(&len, exe + 0x1C, 4);
    off = 0x800;
    if (load != psx_config.load_addr || len != psx_config.load_size)
        psx_log("psx: %s's header says 0x%08X+0x%X, the port was generated for 0x%08X+0x%X\n", path, load, len,
                psx_config.load_addr, psx_config.load_size);
    if (load < PSX_RAM_BASE || load + len > PSX_RAM_BASE + PSX_RAM_SIZE || off + len > size) {
        fprintf(stderr, "psx: the executable's image (0x%08X+0x%X) doesn't fit in RAM\n", load, len);
        exit(65);
    }
    memcpy(psx_host(load), exe + off, len);
    free(exe);
    /* as the start-up code does: clear bss */
    memset(psx_host(psx_config.bss_start), 0, psx_config.bss_end - psx_config.bss_start);
    psx_log("psx: RAM at PS1 0x%08X = host %p (2 MB), %s loaded at 0x%08X (0x%X bytes), bss 0x%08X-0x%08X cleared\n",
            PSX_RAM_BASE, psx_host(PSX_RAM_BASE), psx_config.image, load, len, psx_config.bss_start,
            psx_config.bss_end);
}

/* ---- the PS1 heap: InitHeap(addr, size) gives the BIOS a region of RAM, malloc allocates from it. A first-fit
 * free list with an 8-byte header ({size, next free}) in front of each block, all in RAM, so what it returns is a
 * PS1 address the game can store. */
typedef struct { uint32_t size, next; } HeapBlock; /* size includes the header; next: PS1 address, free list only */
static uint32_t heap_free, heap_lo, heap_hi;

static HeapBlock *blk(uint32_t a) { return (HeapBlock *)psx_host(a); }

void psx_heap_init(uint32_t addr, uint32_t size) {
    addr = (addr + 7) & ~7u;
    size &= ~7u;
    if (size < 16) { heap_free = 0; return; }
    heap_lo = addr;
    heap_hi = addr + size;
    heap_free = addr;
    blk(addr)->size = size;
    blk(addr)->next = 0;
    psx_log("psx: heap at 0x%08X, 0x%X bytes\n", addr, size);
}

void *psx_malloc(uint32_t n) {
    uint32_t need = ((n + 7) & ~7u) + 8, a, *link = &heap_free;
    for (a = heap_free; a; link = &blk(a)->next, a = blk(a)->next) {
        HeapBlock *b = blk(a);
        if (b->size < need) continue;
        if (b->size - need >= 16) { /* split: the tail stays free */
            uint32_t rest = a + need;
            blk(rest)->size = b->size - need;
            blk(rest)->next = b->next;
            b->size = need;
            *link = rest;
        } else {
            *link = b->next;
        }
        b->next = 0;
        return psx_host(a + 8);
    }
    return NULL;
}

void psx_free(void *p) {
    uint32_t a, prev = 0, cur;
    if (!p) return;
    a = psx_ps1(p) - 8;
    if (a < heap_lo || a >= heap_hi) return;
    /* the free list is kept sorted by address, neighbours merged */
    for (cur = heap_free; cur && cur < a; prev = cur, cur = blk(cur)->next) {}
    blk(a)->next = cur;
    if (prev) blk(prev)->next = a;
    else heap_free = a;
    if (cur && a + blk(a)->size == cur) {
        blk(a)->size += blk(cur)->size;
        blk(a)->next = blk(cur)->next;
    }
    if (prev && prev + blk(prev)->size == a) {
        blk(prev)->size += blk(a)->size;
        blk(prev)->next = blk(a)->next;
    }
}
