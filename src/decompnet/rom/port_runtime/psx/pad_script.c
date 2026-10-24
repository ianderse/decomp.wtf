/* A scripted pad, for reproducible runs: PSX_PAD_SCRIPT="start@300,cross@600,circle@700-760" presses a button at a
 * vblank (psx_frames()) for PSX_PAD_SCRIPT_HOLD vblanks (10), or over a range given as FROM-TO (inclusive). Names: up
 * down left right cross circle square triangle l1 l2 r1 r2 start select (any case). The bits are ORed with the
 * keyboard's in psx_pad_bits, which the pad layer (sdk/libpad.c's PadInitDirect buffers, sdk/libetc.c's PadRead)
 * reads every vblank before the game does. */
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "psx_runtime.h"

static const struct { const char *name; uint16_t bit; } buttons[] = {
    {"select", 1 << 0}, {"start", 1 << 3}, {"up", 1 << 4}, {"right", 1 << 5}, {"down", 1 << 6}, {"left", 1 << 7},
    {"l2", 1 << 8}, {"r2", 1 << 9}, {"l1", 1 << 10}, {"r1", 1 << 11}, {"triangle", 1 << 12}, {"circle", 1 << 13},
    {"cross", 1 << 14}, {"square", 1 << 15},
};

static uint16_t button_bit(const char *s, size_t n) {
    size_t i, k;
    for (i = 0; i < sizeof buttons / sizeof buttons[0]; i++) {
        if (strlen(buttons[i].name) != n) continue;
        for (k = 0; k < n && tolower((unsigned char)s[k]) == buttons[i].name[k]; k++) {}
        if (k == n) return buttons[i].bit;
    }
    return 0;
}

/* "name@from[-to]" items separated by commas (spaces allowed): into out[], at most max; the count, or -1 - the index
 * of the first item that doesn't parse */
int psx_pad_script_parse(const char *s, PsxPadPress *out, int max, int hold) {
    int n = 0, item = 0;
    if (hold < 1) hold = 1;
    while (s && *s) {
        const char *name, *at;
        char *end;
        long from, to;
        uint16_t bit;
        while (*s == ' ' || *s == ',') s++;
        if (!*s) break;
        name = s;
        at = strchr(s, '@');
        if (!at) return -1 - item;
        bit = button_bit(name, (size_t)(at - name));
        from = strtol(at + 1, &end, 10);
        if (!bit || end == at + 1 || from < 0) return -1 - item;
        to = from + hold - 1;
        if (*end == '-') {
            const char *t = end + 1;
            to = strtol(t, &end, 10);
            if (end == t || to < from) return -1 - item;
        }
        while (*end == ' ') end++;
        if (*end && *end != ',') return -1 - item;
        if (n < max) {
            out[n].bits = bit;
            out[n].from = (int)from;
            out[n].to = (int)to;
            n++;
        }
        item++;
        s = end;
    }
    return n;
}

uint16_t psx_pad_script_bits(const PsxPadPress *p, int n, int frame) {
    uint16_t bits = 0;
    int i;
    for (i = 0; i < n; i++)
        if (frame >= p[i].from && frame <= p[i].to) bits |= p[i].bits;
    return bits;
}

#define MAX_PRESSES 256
static PsxPadPress presses[MAX_PRESSES];
static int n_presses = -2; /* not read yet */

uint16_t psx_pad_bits(void) {
    uint16_t bits = host_pad();
    if (n_presses == -2) {
        const char *s = getenv("PSX_PAD_SCRIPT"), *h = getenv("PSX_PAD_SCRIPT_HOLD");
        n_presses = 0;
        if (s && *s) {
            int n = psx_pad_script_parse(s, presses, MAX_PRESSES, h && *h ? atoi(h) : 10);
            if (n < 0) psx_log("pad: PSX_PAD_SCRIPT: item %d doesn't parse (name@vblank or name@from-to); ignored\n",
                               -n);
            else {
                int i;
                n_presses = n < MAX_PRESSES ? n : MAX_PRESSES;
                for (i = 0; i < n_presses; i++)
                    psx_log("pad: scripted 0x%04X at vblanks %d-%d\n", presses[i].bits, presses[i].from,
                            presses[i].to);
            }
        }
    }
    return (uint16_t)(bits | psx_pad_script_bits(presses, n_presses, psx_frames()));
}
