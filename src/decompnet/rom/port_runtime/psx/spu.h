/* A software SPU (psx/spu.c), written from psx-spx (https://psx-spx.consoledev.net/soundprocessingunitspu/).
 *
 * The register file is the truth: 0x280 bytes laid out as the hardware's at 0x1F801C00 (the port points it at that
 * address in the window, so a raw store by the game's own C lands in it too; a test harness gives it its own). The
 * mixer reads the voice registers, SPUCNT and the volumes from it at every step, takes the key-on/off bits out of it
 * (KON/KOFF are triggers: consumed and cleared), and writes back what the hardware does: ADSRVOL (each voice's
 * envelope), ENDX, the repeat address a loop-start flag sets, SPUSTAT's mirror of SPUCNT, the current main volume.
 *
 * Threads: spu_mix runs on the audio thread; everything else on the game's. One recursive lock (spu_lock/spu_unlock)
 * covers the voices' state, SPU RAM and the CD queue; libspu/libsnd take it around what they change. Never call the
 * game's code from the audio thread (its stack is the host's, outside the window). */
#ifndef PSX_SPU_H
#define PSX_SPU_H
#include <stdint.h>

#define SPU_RAM_SIZE 0x80000u
#define SPU_RATE 44100
#define SPU_VOICES 24
#define SPU_REG_BYTES 0x280u

/* register offsets from 0x1F801C00 */
#define SPU_V_VOLL 0x0
#define SPU_V_VOLR 0x2
#define SPU_V_PITCH 0x4
#define SPU_V_SSA 0x6 /* start address / 8 */
#define SPU_V_ADSR1 0x8
#define SPU_V_ADSR2 0xA
#define SPU_V_ADSRVOL 0xC
#define SPU_V_LSA 0xE /* repeat address / 8 */
#define SPU_MVOLL 0x180
#define SPU_MVOLR 0x182
#define SPU_RVOLL 0x184
#define SPU_RVOLR 0x186
#define SPU_KON 0x188 /* and 0x18A: voices 16-23 */
#define SPU_KOFF 0x18C
#define SPU_PMON 0x190
#define SPU_NON 0x194
#define SPU_EON 0x198
#define SPU_ENDX 0x19C
#define SPU_RVB_BASE 0x1A2
#define SPU_IRQ_ADDR 0x1A4
#define SPU_XFER_ADDR 0x1A6
#define SPU_XFER_FIFO 0x1A8
#define SPU_SPUCNT 0x1AA
#define SPU_XFER_CTRL 0x1AC
#define SPU_SPUSTAT 0x1AE
#define SPU_CDVOLL 0x1B0
#define SPU_CDVOLR 0x1B2
#define SPU_EXTVOLL 0x1B4
#define SPU_EXTVOLR 0x1B6
#define SPU_CURVOLL 0x1B8
#define SPU_CURVOLR 0x1BA

#define SPUCNT_ENABLE 0x8000
#define SPUCNT_UNMUTE 0x4000
#define SPUCNT_REVERB 0x0080
#define SPUCNT_CD_REVERB 0x0004
#define SPUCNT_CD_AUDIO 0x0001

/* the register file: regs = 0x280 bytes the caller owns (the window's 0x1F801C00 in the port), or NULL for the
 * SPU's own. Resets the voices and SPU RAM; registers are left as they are (spu_reset_regs clears them). */
void spu_init(void *regs);
void spu_reset_regs(void);
uint16_t spu_read(uint32_t off);
void spu_write(uint32_t off, uint16_t v);
volatile uint16_t *spu_regs(void);

/* key on/off now (under the lock), in write order; raw KON/KOFF stores are polled at the next mix step */
void spu_key_on(uint32_t mask);
void spu_key_off(uint32_t mask);
/* key status as libspu reports it: bit 0 keyed on, bit 1 envelope above zero */
int spu_voice_keyed(int v);
int spu_voice_env(int v); /* the envelope level, 0..0x7FFF */

/* SPU RAM (byte addresses, wrapping at 512 KB) */
uint8_t *spu_ram(void);
void spu_ram_write(uint32_t addr, const void *src, uint32_t n);
void spu_ram_read(uint32_t addr, void *dst, uint32_t n);

/* mix `frames` stereo 16-bit frames at 44.1 kHz into out (interleaved L, R) */
void spu_mix(int16_t *out, int frames);

/* a function the mixer calls `hz` times a second of output (libsnd's tick), under the lock; NULL stops it */
void spu_set_tick(void (*fn)(void), int hz);

void spu_lock(void);
void spu_unlock(void);

/* the CD input (XA audio, psx/xa.c): stereo frames already at 44.1 kHz; mixed with the CD volume when SPUCNT's CD
 * audio bit is on. Returns how many frames it took (the queue holds about half a second; the rest is dropped). */
int spu_cd_push(const int16_t *stereo, int frames);
int spu_cd_queued(void);
void spu_cd_clear(void);

/* building blocks, exported for tests: one 16-byte ADPCM block into 28 samples (hist: the decoder's last two
 * outputs, updated); one envelope step of a voice; the 4.12 note-to-pitch ratio. */
void spu_adpcm_decode(const uint8_t block[16], int16_t hist[2], int16_t out[28]);

typedef struct {
    int phase; /* 0 off, 1 attack, 2 decay, 3 sustain, 4 release */
    int32_t level;
    int32_t wait; /* samples left before the next step */
} SpuEnvelope;
void spu_env_step(SpuEnvelope *e, uint16_t adsr1, uint16_t adsr2);

/* XA-ADPCM (psx/xa.c): the CD layer (psx/sdk/libcd.c, which has a weak no-op of it) calls psx_cd_xa_sector with
 * each raw 2352-byte XA audio sector (Form 2, the sync and header included), at the disc's pace; it decodes the 18
 * sound groups (4 or 8 bits, mono or stereo, 37.8 or 18.9 kHz), resamples to 44.1 kHz and queues the result on the
 * CD input. psx_cd_xa_queue does the same and returns the frames queued (0: not an audio sector).
 * psx_cd_xa_reset forgets the decoder's history (a new stream, a seek). */
void psx_cd_xa_sector(const uint8_t *raw2352);
int psx_cd_xa_queue(const uint8_t *raw2352);
void psx_cd_xa_reset(void);
/* decode one sector's sound data (raw + 0x18, 18 x 128 bytes) at its own rate: returns frames, stereo or mono in
 * *channels, the rate in *rate; out holds up to 4032 samples (mono) or 2016 frames x 2 (stereo) */
int xa_decode_sector(const uint8_t *raw2352, int16_t *out, int *channels, int *rate);

/* the audio output (psx/audio.c): SDL2's audio device when the port links SDL2, else a thread that runs the mixer
 * at real time (so libsnd's tick goes on) and, with PSX_AUDIO_WAV=path, writes what it mixes to a WAV file.
 * PSX_AUDIO=0: no output and no thread. */
void psx_audio_start(void);
void psx_audio_stop(void);
#endif
