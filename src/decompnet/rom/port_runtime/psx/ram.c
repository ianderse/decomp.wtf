/* PS1 RAM at its own address: 2 MB at 0x80000000 (KSEG0) and the scratchpad at 0x1F800000. With memory where the game
 * expects it, raw addresses (`*(s32 *)0x8002B590`), 32-bit pointers stored in the game's data and overlay images all
 * work unchanged. Needs the executable linked with a small __PAGEZERO (x86_64; arm64 macOS refuses one under 4 GB). */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include "psx_runtime.h"

static void map_fixed(uint32_t addr, uint32_t size, const char *what) {
    void *want = (void *)(uintptr_t)addr;
    void *p = mmap(want, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0);
    if (p == MAP_FAILED || p != want) {
        fprintf(stderr, "psx: can't map %s at 0x%08X (%s). The executable must be linked with "
                        "-Wl,-pagezero_size,0x1000 for x86_64 (arm64 macOS refuses a __PAGEZERO under 4 GB).\n",
                what, addr, strerror(errno));
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
    map_fixed(PSX_RAM_BASE, PSX_RAM_SIZE, "PS1 RAM");
    map_fixed(PSX_SCRATCH_BASE, PSX_SCRATCH_SIZE, "the scratchpad");
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
    memcpy((void *)(uintptr_t)load, exe + off, len);
    free(exe);
    /* as the start-up code does: clear bss */
    memset((void *)(uintptr_t)psx_config.bss_start, 0, psx_config.bss_end - psx_config.bss_start);
    psx_log("psx: RAM at 0x%08X (2 MB), %s loaded at 0x%08X (0x%X bytes), bss 0x%08X-0x%08X cleared\n", PSX_RAM_BASE,
            psx_config.image, load, len, psx_config.bss_start, psx_config.bss_end);
}
