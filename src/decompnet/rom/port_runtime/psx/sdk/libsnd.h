/* libsnd: what libsnd.c implements on the host (the VAB banks, the SEQ sequencer, the voice manager), with
 * PSY-Q's names and the constants the game passes. */
#ifndef PSX_LIBSND_H
#define PSX_LIBSND_H

#define SSPLAY_PAUSE 0
#define SSPLAY_PLAY 1
#define SSPLAY_INFINITY 0
#define SS_NOTICK0 0
#define SS_TICK60 1
#define SS_TICK240 2
#define SS_TICK120 3
#define SS_TICK50 4
#define SS_TICKVSYNC 5
#define SS_NOTICK 0x1000
#define SS_SERIAL_A 0
#define SS_SERIAL_B 1
#define SS_MIX 0
#define SS_REV 1
#define SS_SOFF 0
#define SS_SON 1

void SsInit(void);
void SsInitHot(void);
void SsSetTableSize(char *table, short s_max, short t_max);
void SsSetTickMode(int mode);
void SsStart(void);
void SsStart2(void);
void SsEnd(void);
void SsQuit(void);
void SsSeqCalledTbyT(void);
void SsSetMono(void);
void SsSetStereo(void);
void SsSetReservedVoice(char n);
void SsSetMVol(short l, short r);
void SsSetRVol(short l, short r);
void SsSetSerialAttr(char s_num, char attr, char mode);
void SsSetSerialVol(char s_num, short l, short r);
short SsVabOpenHead(unsigned char *addr, short vabid);
short SsVabOpenHeadSticky(unsigned char *addr, short vabid, unsigned int sbaddr);
short SsVabTransBody(unsigned char *addr, short vabid);
short SsVabTransBodyPartly(unsigned char *addr, unsigned int bufsize, short vabid);
short SsVabTransCompleted(short flag);
short SsVabTransfer(unsigned char *vh, unsigned char *vb, short vabid, short flag);
void SsVabClose(short vabid);
short SsSeqOpen(unsigned int *addr, short vabid);
void SsSeqClose(short seq);
void SsSeqPlay(short seq, char mode, short l_count);
void SsSeqStop(short seq);
void SsSeqPause(short seq);
void SsSeqReplay(short seq);
void SsSeqSetVol(short seq, short l, short r);
void SsSeqGetVol(short seq, short tr, short *l, short *r);
short SsIsEos(short seq, short tr);
void SsPlayBack(short seq, short tr, short l_count);
short SsVoKeyOn(int vab_pro, int pitch, unsigned short voll, unsigned short volr);
short SsVoKeyOff(int vab_pro, int pitch);
short SsUtKeyOn(short vab, short prog, short tone, short note, short fine, short voll, short volr);
short SsUtKeyOff(short voice, short vab, short prog, short tone, short note);
short SsUtKeyOffV(short voice);
void SsUtAllKeyOff(short mode);
short SsUtSetVVol(short voice, short l, short r);
void SsUtReverbOn(void);
void SsUtReverbOff(void);
short SsUtSetReverbType(short type);
void SsUtSetReverbDepth(short l, short r);
void SsUtSetReverbFeedback(short feedback);
void SsUtSetReverbDelay(short delay);
short SpuVmSetProgVol(short vab, short prog, unsigned char vol);
short SpuVmGetProgVol(short vab, short prog);
short SpuVmSetProgPan(short vab, short prog, unsigned char pan);
short SpuVmGetProgPan(short vab, short prog);
#endif
