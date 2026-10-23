/* libcd over the disc image (libcd.c on psx/cd.c). The PSY-Q types, command numbers and mode/status bits below are
 * PsyCross's include/psx/libcd.h (MIT, Copyright (c) 2020 REDRIVER2 Project; psx/sdk/LICENSE.PsyCross), with its
 * types made the PS1's (u_long is 32 bits there); the functions are this port's.
 *
 * The drive model, deterministic per frame:
 *  - CdControl/CdControlF/CdControlB issue a command. Its result (stat, and for GetlocL/GetlocP/GetTN/GetTD/
 *    Getparam/GetID their bytes) is written back at once; its completion (CdlComplete) is delivered at the next
 *    "CD event point": a VSync (psx_cd_vblank), or a CdSync/CdReady/CdReadSync/CdDataSync poll. The sync callback
 *    (CdSyncCallback) fires there. CdControlB and CdSync(0) wait for it.
 *  - A read (CdlReadN/CdlReadS, or CdRead) moves the head one sector per sector of credit: a VSync gives 2.5 sectors
 *    at double speed (CdlModeSpeed) and 1.25 at single, as the drive's 150/75 sectors a second; a poll gives one, so
 *    a loop that only polls still finishes; a blocking wait (CdReadSync(0), CdReady(0)) runs to the end. PSX_CD_FAST=1
 *    delivers a CdRead's sectors all at the next event point.
 *  - Each sector read: for a CdRead, its data (2048 bytes, or 2340/2328 with CdlModeSize1/Size0) goes straight into
 *    the game's buffer in RAM; at the end the read callback (CdReadCallback) fires with CdlComplete and the drive
 *    pauses. Otherwise it becomes the current sector: CdReady reports CdlDataReady once, the ready callback
 *    (CdReadyCallback) fires with it, CdGetSector copies from it (successive calls go on where the last stopped),
 *    and the data callback (CdDataCallback) fires after a CdGetSector inside a ready callback, as the DMA's end did.
 *    With CdlModeRT, a Form 2 audio sector (XA ADPCM; the channel filter with CdlModeSF) goes to
 *    psx_cd_xa_sector() instead and is not delivered to the CPU. CdRead2 with CdlModeStream reads into the St ring.
 *  - Every pointer the game passes is a PS1 address (psx_host() turns it into a host pointer); callbacks are PS1
 *    function addresses, called through psx_host_fn() with a result buffer in the window. */
#ifndef PSX_LIBCD_H
#define PSX_LIBCD_H
#include <stdint.h>

#define CdlModeStream 0x100  /* Normal Streaming */
#define CdlModeStream2 0x120 /* SUB HEADER information includes */
#define CdlModeSpeed 0x80    /* 0: normal speed 1: double speed */
#define CdlModeRT 0x40       /* 0: ADPCM off 1: ADPCM on */
#define CdlModeSize1 0x20    /* 0: 2048 byte 1: 2340byte */
#define CdlModeSize0 0x10    /* 0: - 1: 2328byte */
#define CdlModeSF 0x08       /* 0: Channel off 1: Channel on */
#define CdlModeRept 0x04     /* 0: Report off 1: Report on */
#define CdlModeAP 0x02       /* 0: AutoPause off 1: AutoPause on */
#define CdlModeDA 0x01       /* 0: CD-DA off 1: CD-DA on */

#define CdlStatPlay 0x80      /* playing CD-DA */
#define CdlStatSeek 0x40      /* seeking */
#define CdlStatRead 0x20      /* reading data sectors */
#define CdlStatShellOpen 0x10 /* once shell open */
#define CdlStatSeekError 0x04 /* seek error detected */
#define CdlStatStandby 0x02   /* spindle motor rotating */
#define CdlStatError 0x01     /* command error detected */

#define CdlNop 0x01
#define CdlSetloc 0x02
#define CdlPlay 0x03
#define CdlForward 0x04
#define CdlBackward 0x05
#define CdlReadN 0x06
#define CdlStandby 0x07
#define CdlStop 0x08
#define CdlPause 0x09
#define CdlInit 0x0a /* not in PsyCross's list */
#define CdlMute 0x0b
#define CdlDemute 0x0c
#define CdlSetfilter 0x0d
#define CdlSetmode 0x0e
#define CdlGetparam 0x0f
#define CdlGetlocL 0x10
#define CdlGetlocP 0x11
#define CdlGetTN 0x13
#define CdlGetTD 0x14
#define CdlSeekL 0x15
#define CdlSeekP 0x16
#define CdlTest 0x19   /* not in PsyCross's list */
#define CdlGetID 0x1a  /* not in PsyCross's list */
#define CdlReadS 0x1B
#define CdlReset 0x1c  /* not in PsyCross's list */
#define CdlReadTOC 0x1e /* not in PsyCross's list */

#define CdlNoIntr 0x00      /* No interrupt */
#define CdlDataReady 0x01   /* Data Ready */
#define CdlComplete 0x02    /* Command Complete */
#define CdlAcknowledge 0x03 /* Acknowledge (reserved) */
#define CdlDataEnd 0x04     /* End of Data Detected */
#define CdlDiskError 0x05   /* Error Detected */

typedef struct {
    uint8_t minute; /* minute (BCD) */
    uint8_t second; /* second (BCD) */
    uint8_t sector; /* sector (BCD) */
    uint8_t track;  /* track (void) */
} CdlLOC;

typedef struct {
    uint8_t file; /* file ID (always 1) */
    uint8_t chan; /* channel ID */
    uint16_t pad;
} CdlFILTER;

typedef struct {
    uint8_t val0; /* volume for CD(L) -> SPU (L) */
    uint8_t val1; /* volume for CD(L) -> SPU (R) */
    uint8_t val2; /* volume for CD(R) -> SPU (L) */
    uint8_t val3; /* volume for CD(R) -> SPU (R) */
} CdlATV;

typedef struct {
    CdlLOC pos;    /* file location */
    uint32_t size; /* file size */
    char name[16]; /* file name (body) */
} CdlFILE;

typedef struct { /* the 32-byte header of an STR (MDEC movie) sector */
    uint16_t id;
    uint16_t type;
    uint16_t secCount;
    uint16_t nSectors;
    uint32_t frameCount;
    uint32_t frameSize;
    uint16_t width;
    uint16_t height;
    uint32_t dummy1;
    uint32_t dummy2;
    CdlLOC loc;
} StHEADER;

#define StFREE 0x0000
#define StREWIND 0x0001
#define StCOMPLETE 0x0002
#define StBUSY 0x0003
#define StLOCK 0x0004

/* ---- the API. A pointer parameter is the game's PS1 address (uint32_t); a callback is a PS1 function address.
 * Parameters are int/uint32_t (never narrower): the game's own prototypes vary, and gen/'s thunks pass them on. */
int CdInit(void);
int CdReset(int mode);
void CdFlush(void);
int CdStatus(void);
int CdMode(void);
int CdLastCom(void);
int CdSetDebug(int level);
int CdSync(int mode, uint32_t result);
int CdReady(int mode, uint32_t result);
uint32_t CdSyncCallback(uint32_t func);
uint32_t CdReadyCallback(uint32_t func);
uint32_t CdReadCallback(uint32_t func);
uint32_t CdDataCallback(uint32_t func);
int CdControl(int com, uint32_t param, uint32_t result);
int CdControlF(int com, uint32_t param);
int CdControlB(int com, uint32_t param, uint32_t result);
int psx_CdControlCore(int com, uint32_t param, uint32_t result, int nowait); /* libcd's internal (CD_cw with retry) */
int CdMix(uint32_t vol);
int CdGetSector(uint32_t madr, int words);
int CdGetSector2(uint32_t madr, int words);
int CdDataSync(int mode);
uint32_t CdIntToPos(int i, uint32_t p);
int CdPosToInt(uint32_t p);
uint32_t CdSearchFile(uint32_t fp, uint32_t name);
int CdRead(int sectors, uint32_t buf, int mode);
int CdReadSync(int mode, uint32_t result);
int CdRead2(int mode);
int CdReadFile(uint32_t file, uint32_t addr, int nbyte);
int CdDiskReady(int mode);
int CdGetDiskType(void);

void StSetRing(uint32_t ring_addr, uint32_t ring_size);
void StClearRing(void);
void StUnSetRing(void);
void StSetStream(uint32_t mode, uint32_t start_frame, uint32_t end_frame, uint32_t func1, uint32_t func2);
void StSetEmulate(uint32_t addr, uint32_t mode, uint32_t start_frame, uint32_t end_frame, uint32_t func1,
                  uint32_t func2);
uint32_t StFreeRing(uint32_t base);
uint32_t StGetNext(uint32_t addr, uint32_t header);
int StSetChannel(uint32_t channel);
void StSetMask(uint32_t mask, uint32_t start, uint32_t end);

/* the runtime's side: the VSync path calls this once a frame (a CD event point) */
void psx_cd_vblank(void);
/* every XA ADPCM sector the drive plays (CdlModeRT and a Form 2 audio sector passing the channel filter): the whole
 * 2352-byte raw sector (sub-header at +16: file, channel, submode, coding info). A weak no-op here until the SPU's
 * implementation (a strong definition) is linked in. The CD's volume (CdMix) is psx_cd_mix below. */
void psx_cd_xa_sector(const uint8_t *raw2352);
extern CdlATV psx_cd_mix;
extern int psx_cd_muted;
#endif
