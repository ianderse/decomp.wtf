/* The disc image, read in place (cd.h). Written for the port from ECMA-119 (ISO 9660) and psx-spx's CD-ROM format
 * notes; the format detection follows decompnet/rom/psx.py's reader (the volume descriptor's "CD001" at sector 16
 * for each sector size). PsyCross's libcd (MIT) reads its images with a fixed 2352-byte sector and looks files up
 * in the first sector of the root directory at LBA 22 only; this one detects the layout and walks the whole tree.
 * Nothing here knows about the PS1's memory: callers copy out of the cache. */
#include "cd.h"

#include <ctype.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CACHE 64
#define MAX_FILES 8192
#define MAX_DEPTH 8

typedef struct {
    char *path;
    uint32_t lba, size;
} DiscFile;

static struct {
    int fd;
    char path[4096];
    int size;      /* bytes per sector in the image */
    uint32_t count;
    uint8_t cache[CACHE][PSX_CD_RAW];
    uint32_t tag[CACHE]; /* lba + 1, 0 = empty */
    DiscFile *files;     /* sorted by lba */
    int nfiles;
    char boot[64];
} D = {.fd = -1};

void psx_lba_to_msf(uint32_t lba, uint8_t msf[3]) {
    uint32_t a = lba + 150;
    msf[0] = PSX_BCD(a / 4500 % 100);
    msf[1] = PSX_BCD(a / 75 % 60);
    msf[2] = PSX_BCD(a % 75);
}

uint32_t psx_msf_to_lba(const uint8_t msf[3]) {
    uint32_t a = (PSX_UNBCD(msf[0]) * 60 + PSX_UNBCD(msf[1])) * 75 + PSX_UNBCD(msf[2]);
    return a >= 150 ? a - 150 : 0;
}

static int get(uint64_t at, void *dst, size_t n) {
    return pread(D.fd, dst, n, (off_t)at) == (ssize_t)n;
}

int psx_disc_is_open(void) { return D.fd >= 0; }
const char *psx_disc_path(void) { return D.fd >= 0 ? D.path : ""; }
uint32_t psx_disc_sectors(void) { return D.fd >= 0 ? D.count : 0; }
int psx_disc_sector_size(void) { return D.fd >= 0 ? D.size : 0; }
const char *psx_disc_boot(void) { return D.boot; }

const uint8_t *psx_disc_raw(uint32_t lba) {
    int slot = (int)(lba % CACHE);
    uint8_t *s = D.cache[slot];
    if (D.fd < 0 || lba >= D.count) return NULL;
    if (D.tag[slot] == lba + 1) return s;
    if (D.size == 2048) {
        static const uint8_t sync[12] = {0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0};
        memset(s, 0, PSX_CD_RAW);
        if (!get((uint64_t)lba * 2048, s + 24, 2048)) return NULL;
        memcpy(s, sync, 12);
        psx_lba_to_msf(lba, s + 12);
        s[15] = 2;
        s[18] = s[22] = 0x08; /* sub-header: data, Form 1 */
    } else if (!get((uint64_t)lba * (uint64_t)D.size, s, PSX_CD_RAW)) {
        return NULL;
    }
    D.tag[slot] = lba + 1;
    return s;
}

int psx_disc_user(const uint8_t *raw, int *offset) {
    if (raw[15] == 1) { *offset = 16; return PSX_CD_DATA; }
    if (raw[15] == 2) { *offset = 24; return (raw[18] & 0x20) ? PSX_CD_FORM2 : PSX_CD_DATA; }
    *offset = 0;
    return 0;
}

int psx_disc_read(uint32_t lba, void *dst, int n) {
    int i, off;
    for (i = 0; i < n; i++) {
        const uint8_t *r = psx_disc_raw(lba + (uint32_t)i);
        if (!r || !psx_disc_user(r, &off)) break;
        memcpy((uint8_t *)dst + (size_t)i * PSX_CD_DATA, r + off, PSX_CD_DATA);
    }
    return i;
}

/* ---- ISO 9660 */
typedef struct {
    uint32_t lba, size;
    int dir;
    char name[256]; /* without ";1", as on the disc */
} Rec;

/* the directory at lba (size bytes): calls fn for each record but "." and ".." */
static int each(uint32_t lba, uint32_t size, int (*fn)(const Rec *, void *), void *ctx) {
    uint32_t done = 0;
    uint8_t sec[PSX_CD_DATA];
    if (size > 64u * 1024 * 1024) return 0;
    while (done < size) {
        uint32_t i = 0;
        if (psx_disc_read(lba + done / PSX_CD_DATA, sec, 1) != 1) return 0;
        while (i < PSX_CD_DATA) {
            uint8_t n = sec[i];
            Rec r;
            int len;
            if (n == 0 || i + n > PSX_CD_DATA || n < 34) break; /* records don't cross sectors: the rest is padding */
            len = sec[i + 32];
            if (len > 0 && 33 + len <= n && !(len == 1 && sec[i + 33] <= 1)) {
                memcpy(r.name, sec + i + 33, (size_t)len);
                r.name[len] = 0;
                if (strchr(r.name, ';')) *strchr(r.name, ';') = 0;
                if (len > 1 && r.name[strlen(r.name) - 1] == '.') r.name[strlen(r.name) - 1] = 0; /* "FILE." */
                r.lba = (uint32_t)sec[i + 2] | (uint32_t)sec[i + 3] << 8 | (uint32_t)sec[i + 4] << 16 |
                        (uint32_t)sec[i + 5] << 24;
                r.size = (uint32_t)sec[i + 10] | (uint32_t)sec[i + 11] << 8 | (uint32_t)sec[i + 12] << 16 |
                         (uint32_t)sec[i + 13] << 24;
                r.dir = (sec[i + 25] & 2) != 0;
                if (fn(&r, ctx)) return 1;
            }
            i += n;
        }
        done += PSX_CD_DATA;
    }
    return 0;
}

static int root(uint32_t *lba, uint32_t *size) {
    uint8_t pvd[PSX_CD_DATA];
    if (psx_disc_read(16, pvd, 1) != 1 || pvd[0] != 1 || memcmp(pvd + 1, "CD001", 5) != 0) return 0;
    *lba = (uint32_t)pvd[158] | (uint32_t)pvd[159] << 8 | (uint32_t)pvd[160] << 16 | (uint32_t)pvd[161] << 24;
    *size = (uint32_t)pvd[166] | (uint32_t)pvd[167] << 8 | (uint32_t)pvd[168] << 16 | (uint32_t)pvd[169] << 24;
    return 1;
}

typedef struct {
    const char *want;
    Rec hit;
} Find;

static int name_eq(const char *a, const char *b) {
    for (; *a && *b; a++, b++)
        if (toupper((unsigned char)*a) != toupper((unsigned char)*b)) return 0;
    return *a == *b;
}

static int find_one(const Rec *r, void *ctx) {
    Find *f = ctx;
    if (!name_eq(r->name, f->want)) return 0;
    f->hit = *r;
    return 1;
}

int psx_disc_find(const char *path, uint32_t *lba, uint32_t *size) {
    char part[256];
    const char *p = path;
    uint32_t at, len;
    int is_dir = 1;
    if (D.fd < 0 || !path || !root(&at, &len)) return 0;
    if (!strncasecmp(p, "cdrom:", 6)) p += 6;
    for (;;) {
        size_t n = 0;
        Find f;
        while (*p == '\\' || *p == '/') p++;
        if (!*p) break;
        while (p[n] && p[n] != '\\' && p[n] != '/' && p[n] != ';') n++;
        if (n == 0 || n >= sizeof part || !is_dir) return 0;
        memcpy(part, p, n);
        part[n] = 0;
        if (part[n - 1] == '.') part[n - 1] = 0;
        p += n;
        if (*p == ';') while (*p && *p != '\\' && *p != '/') p++; /* the version */
        f.want = part;
        if (!each(at, len, find_one, &f)) return 0;
        at = f.hit.lba;
        len = f.hit.size;
        is_dir = f.hit.dir;
    }
    if (is_dir) return 0;
    *lba = at;
    *size = len;
    return 1;
}

/* ---- every file, for psx_disc_name_at (the logs say which file a read is in) */
typedef struct {
    char prefix[512];
    int depth;
} Walk;

static int walk_one(const Rec *r, void *ctx) {
    Walk *w = ctx;
    char full[768];
    snprintf(full, sizeof full, "%s%s%s", w->prefix, *w->prefix ? "/" : "", r->name);
    if (r->dir) {
        Walk sub;
        if (w->depth >= MAX_DEPTH) return 0;
        snprintf(sub.prefix, sizeof sub.prefix, "%s", full);
        sub.depth = w->depth + 1;
        each(r->lba, r->size, walk_one, &sub);
    } else if (D.nfiles < MAX_FILES) {
        D.files[D.nfiles].path = strdup(full);
        D.files[D.nfiles].lba = r->lba;
        D.files[D.nfiles].size = r->size;
        D.nfiles++;
    }
    return 0;
}

static int by_lba(const void *a, const void *b) {
    const DiscFile *x = a, *y = b;
    return x->lba < y->lba ? -1 : x->lba > y->lba;
}

const char *psx_disc_name_at(uint32_t lba, uint32_t *offset) {
    int lo = 0, hi = D.nfiles - 1, best = -1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (D.files[mid].lba <= lba) { best = mid; lo = mid + 1; }
        else hi = mid - 1;
    }
    if (best < 0) return NULL;
    if ((uint64_t)(lba - D.files[best].lba) * PSX_CD_DATA >= (D.files[best].size ? D.files[best].size : 1)) {
        return NULL;
    }
    if (offset) *offset = (lba - D.files[best].lba) * PSX_CD_DATA;
    return D.files[best].path;
}

static void read_boot(void) {
    uint32_t lba, size;
    char cnf[2049], *b, *e;
    D.boot[0] = 0;
    if (!psx_disc_find("SYSTEM.CNF", &lba, &size) || psx_disc_read(lba, cnf, 1) != 1) return;
    cnf[size < 2048 ? size : 2048] = 0;
    b = strstr(cnf, "BOOT");
    if (!b || !(b = strchr(b, '='))) return;
    b++;
    while (*b == ' ' || *b == '\t') b++;
    if (!strncasecmp(b, "cdrom:", 6)) b += 6;
    while (*b == '\\' || *b == '/') b++;
    for (e = b; *e && *e != ';' && *e != '\r' && *e != '\n' && *e != ' '; e++) {}
    snprintf(D.boot, sizeof D.boot, "%.*s", (int)(e - b), b);
}

void psx_disc_close(void) {
    int i;
    if (D.fd >= 0) close(D.fd);
    for (i = 0; i < D.nfiles; i++) free(D.files[i].path);
    free(D.files);
    D.files = NULL;
    D.nfiles = 0;
    D.fd = -1;
    D.boot[0] = 0;
    memset(D.tag, 0, sizeof D.tag);
}

/* a .cue's first FILE "name" BINARY, relative to the .cue */
static int from_cue(const char *cue, char *out, size_t n) {
    FILE *f = fopen(cue, "r");
    char line[4096];
    const char *slash = strrchr(cue, '/');
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        char *p = line, *q;
        while (isspace((unsigned char)*p)) p++;
        if (strncasecmp(p, "FILE", 4) != 0) continue;
        p += 4;
        while (isspace((unsigned char)*p)) p++;
        if (*p == '"') { p++; q = strchr(p, '"'); }
        else { q = p; while (*q && !isspace((unsigned char)*q)) q++; }
        if (!q) break;
        *q = 0;
        if (*p == '/' || !slash) snprintf(out, n, "%s", p);
        else snprintf(out, n, "%.*s/%s", (int)(slash - cue), cue, p);
        fclose(f);
        return 1;
    }
    fclose(f);
    return 0;
}

int psx_disc_open(const char *path, char *err, size_t errlen) {
    static const int sizes[][2] = {{2048, 0}, {2352, 24}, {2352, 16}, {2448, 24}, {2448, 16}};
    char real[4096];
    size_t i, len = path ? strlen(path) : 0;
    off_t end;
    psx_disc_close();
    if (!len) { snprintf(err, errlen, "no disc image given"); return 0; }
    if (len > 4 && !strcasecmp(path + len - 4, ".chd")) {
        snprintf(err, errlen, "%s is a CHD: convert it with `chdman extractcd -i in.chd -o out.cue` and use the .bin",
                 path);
        return 0;
    }
    snprintf(real, sizeof real, "%s", path);
    if (len > 4 && !strcasecmp(path + len - 4, ".cue") && !from_cue(path, real, sizeof real)) {
        snprintf(err, errlen, "%s: no FILE line in the cue sheet", path);
        return 0;
    }
    D.fd = open(real, O_RDONLY);
    if (D.fd < 0) { snprintf(err, errlen, "can't open %s", real); return 0; }
    end = lseek(D.fd, 0, SEEK_END);
    for (i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
        char id[6] = {0};
        if (get((uint64_t)16 * (uint64_t)sizes[i][0] + (uint64_t)sizes[i][1] + 1, id, 5) && !memcmp(id, "CD001", 5)) {
            D.size = sizes[i][0];
            break;
        }
    }
    if (i == sizeof sizes / sizeof sizes[0]) {
        snprintf(err, errlen, "%s isn't an ISO 9660 disc image this port reads (2352-byte raw .bin or 2048-byte .iso)",
                 real);
        close(D.fd);
        D.fd = -1;
        return 0;
    }
    snprintf(D.path, sizeof D.path, "%s", real);
    D.count = (uint32_t)(end / D.size);
    D.files = calloc(MAX_FILES, sizeof *D.files);
    {
        uint32_t at, n;
        Walk w = {"", 0};
        if (root(&at, &n)) each(at, n, walk_one, &w);
    }
    qsort(D.files, (size_t)D.nfiles, sizeof *D.files, by_lba);
    read_boot();
    return 1;
}
