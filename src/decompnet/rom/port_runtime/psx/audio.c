/* The audio output: the SPU's mix (44.1 kHz stereo 16-bit) to SDL2's audio device when the port links SDL2
 * (PSX_AUDIO_SDL, set by CMake for PSX_HOST=sdl), else to a thread that runs the mixer at real time, so libsnd's
 * tick and the game's waits on it still go on, and with PSX_AUDIO_WAV=path writes the mix to a WAV file.
 * PSX_AUDIO=0: neither (the SPU is still there, nothing pulls it). PSX_HEADLESS runs use the thread unless
 * PSX_AUDIO=sdl. */
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "psx_runtime.h"
#include "spu.h"

#ifdef PSX_AUDIO_SDL
#include <SDL.h>
static SDL_AudioDeviceID dev;
static void sdl_callback(void *user, Uint8 *stream, int len) {
    (void)user;
    spu_mix((int16_t *)stream, len / 4);
}
#endif

static pthread_t thread;
static volatile int running;
static FILE *wav;
static uint32_t wav_frames;

static void wav_header(FILE *f, uint32_t frames) {
    uint8_t h[44];
    uint32_t data = frames * 4, v;
    memcpy(h, "RIFF", 4);
    v = 36 + data; memcpy(h + 4, &v, 4);
    memcpy(h + 8, "WAVEfmt ", 8);
    v = 16; memcpy(h + 16, &v, 4);
    h[20] = 1; h[21] = 0; h[22] = 2; h[23] = 0; /* PCM, stereo */
    v = SPU_RATE; memcpy(h + 24, &v, 4);
    v = SPU_RATE * 4; memcpy(h + 28, &v, 4);
    h[32] = 4; h[33] = 0; h[34] = 16; h[35] = 0;
    memcpy(h + 36, "data", 4);
    memcpy(h + 40, &data, 4);
    fseek(f, 0, SEEK_SET);
    fwrite(h, 1, sizeof h, f);
    fseek(f, 0, SEEK_END);
}

static void *pace(void *arg) {
    int16_t buf[735 * 2]; /* 1/60 s */
    struct timespec next;
    (void)arg;
    clock_gettime(CLOCK_MONOTONIC, &next);
    while (running) {
        struct timespec now, d;
        spu_mix(buf, 735);
        if (wav) {
            fwrite(buf, 4, 735, wav);
            wav_frames += 735;
            if (wav_frames % (735 * 60) == 0) { /* a run that ends with _exit (PSX_TIMEOUT, a fault) keeps a valid file */
                wav_header(wav, wav_frames);
                fflush(wav);
            }
        }
        next.tv_nsec += 16666667L;
        if (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        clock_gettime(CLOCK_MONOTONIC, &now);
        d.tv_sec = next.tv_sec - now.tv_sec;
        d.tv_nsec = next.tv_nsec - now.tv_nsec;
        if (d.tv_nsec < 0) { d.tv_nsec += 1000000000L; d.tv_sec--; }
        if (d.tv_sec >= 0) nanosleep(&d, NULL);
        else next = now; /* far behind: don't race */
    }
    return NULL;
}

void psx_audio_start(void) {
    static int started;
    const char *mode = getenv("PSX_AUDIO"), *headless = getenv("PSX_HEADLESS"), *wav_path = getenv("PSX_AUDIO_WAV");
    int is_headless = headless && *headless && strcmp(headless, "0") != 0;
    if (started++) return;
    if (mode && strcmp(mode, "0") == 0) {
        psx_log("audio: off (PSX_AUDIO=0)\n");
        return;
    }
#ifdef PSX_AUDIO_SDL
    if (!wav_path && (!is_headless || (mode && strcmp(mode, "sdl") == 0))) {
        SDL_AudioSpec want, have;
        if (SDL_InitSubSystem(SDL_INIT_AUDIO) == 0) {
            memset(&want, 0, sizeof want);
            want.freq = SPU_RATE;
            want.format = AUDIO_S16SYS;
            want.channels = 2;
            want.samples = 512;
            want.callback = sdl_callback;
            dev = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);
            if (dev) {
                SDL_PauseAudioDevice(dev, 0);
                psx_log("audio: SDL2, %d Hz, %d-frame buffer\n", have.freq, have.samples);
                atexit(psx_audio_stop);
                return;
            }
        }
        psx_log("audio: SDL2 has no audio device (%s); mixing without output\n", SDL_GetError());
    }
#endif
    (void)is_headless;
    if (wav_path && *wav_path) {
        wav = fopen(wav_path, "wb");
        if (wav) wav_header(wav, 0);
    }
    running = 1;
    if (pthread_create(&thread, NULL, pace, NULL) != 0) {
        running = 0;
        return;
    }
    psx_log("audio: no output device; the mixer runs at real time%s%s\n", wav ? ", written to " : "",
            wav ? wav_path : "");
    atexit(psx_audio_stop);
}

void psx_audio_stop(void) {
#ifdef PSX_AUDIO_SDL
    if (dev) {
        SDL_CloseAudioDevice(dev);
        dev = 0;
    }
#endif
    if (running) {
        running = 0;
        pthread_join(thread, NULL);
    }
    if (wav) {
        wav_header(wav, wav_frames);
        fclose(wav);
        wav = NULL;
    }
}
