/* What libsnd.c uses of libspu.c: the same operations as the Sony functions, on host pointers (the Sony-named
 * entry points take the game's PS1 pointers and translate them; these don't, so libsnd can pass its own structs
 * and run on the audio thread). */
#ifndef PSX_SOUND_H
#define PSX_SOUND_H
#include <stdint.h>

#include "libspu.h"

#ifndef PSX_SDK_WEAK /* a Sony function the game may have as matched C: the game's definition wins at link */
#define PSX_SDK_WEAK __attribute__((weak))
#endif

void psx_spu_start(void); /* SpuInit: the SPU at 0x1F801C00 in the window, reset, enabled; the audio output */
void psx_spu_set_voice_attr(const SpuVoiceAttr *a);
void psx_spu_get_voice_attr(SpuVoiceAttr *a);
void psx_spu_set_common_attr(const SpuCommonAttr *a);
void psx_spu_get_common_attr(SpuCommonAttr *a);
int psx_spu_init_malloc(int num);
int psx_spu_malloc(int size);
int psx_spu_malloc_at(uint32_t addr, int size);
void psx_spu_free(uint32_t addr);
uint32_t psx_spu_set_transfer_addr(uint32_t addr);
uint32_t psx_spu_write(const void *src, uint32_t size); /* at the transfer address */
int psx_spu_set_reverb(int on);
uint32_t psx_spu_set_reverb_voice(int on, uint32_t bits);
void psx_spu_set_reverb_depth(int16_t l, int16_t r);
int psx_spu_set_reverb_mode(int mode);
/* (note << 8 | fine) played from a sample whose own note is (center << 8 | fine): fine in 1/128 semitone, 0x1000 = the
 * sample's own rate */
uint16_t psx_spu_note_pitch(uint16_t sample_note, uint16_t note);
/* where SpuInit puts the register file: NULL (the default) is the window's 0x1F801C00; a test harness without the
 * window gives a buffer of its own */
extern void *psx_spu_io;

/* libsnd's core on host pointers (libsnd.c): a VAB header and body, a SEQ, played by the tick */
int psx_snd_vab_open_head(const uint8_t *vh, int vabid);
int psx_snd_vab_trans_body(const uint8_t *vb, int vabid);
void psx_snd_vab_close(int vabid);
int psx_snd_seq_open(const uint8_t *seq, int vabid);
void psx_snd_seq_play(int seq, int mode, int l_count);
void psx_snd_seq_stop(int seq);
void psx_snd_seq_set_vol(int seq, int l, int r);
void psx_snd_tick(void);           /* one call of SsSeqCalledTbyT's tick */
int psx_snd_voice_of(int seq, int ch, int note); /* the SPU voice playing it, or -1 (tests) */
#endif
