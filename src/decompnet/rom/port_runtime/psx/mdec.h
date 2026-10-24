/* The MDEC (the PS1's macroblock decoder) in software, and the Huffman stage in front of it that libpress's
 * DecDCTvlc does on the CPU. Written from psx-spx's "Macroblock Decoder (MDEC)" and "CDROM File Video BS
 * Compression" sections: the run-length format, rl_decode_block, the IDCT with the MDEC(3) scale table, yuv_to_rgb
 * and y_to_mono, the BS v1/v2/v3 headers, DC and AC Huffman codes. No emulator's code. Plain C over host memory:
 * sdk/libpress.c puts it under libpress's API on PS1 RAM, and tests/test_port_mdec.py compiles it on its own. */
#ifndef PSX_MDEC_H
#define PSX_MDEC_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t iq_y[64];  /* MDEC(2): the luminance quant table (Y1..Y4), in zigzag order */
    uint8_t iq_c[64];  /* and the colour one (Cr, Cb) */
    int16_t scale[64]; /* MDEC(3): the IDCT matrix, 14-bit fraction (the standard JPEG constants) */
} MdecTables;

/* libpress's DecDCTReset tables: psx-spx's standard scale matrix, and the quant table Sony's library uploads (the
 * MPEG-1 default intra matrix in zigzag order with 2 as its DC entry), for both luminance and colour */
void mdec_default_tables(MdecTables *t);

/* zigzag[raster index] = zigzag position (psx-spx), and its inverse */
extern const uint8_t mdec_zigzag[64];
extern const uint8_t mdec_zagzig[64];

/* the IDCT of one 8x8 block in raster order, in place: two passes of psx-spx's real_idct_core (the scale table's
 * upper 13 bits, sum rounded to the nearest at 13 fraction bits) */
void mdec_idct(const int16_t scale[64], int32_t blk[64]);

/* one block from run-length halfwords (rl_decode_block): FE00h padding before the DC skipped, DC with its quant
 * scale, AC run/level pairs to FE00h; dequantised with qt, IDCT'd into blk. *pos advances; returns 0 when the data
 * ran out before a block was complete. */
int mdec_rl_block(const MdecTables *t, const uint8_t qt[64], const uint16_t *src, int n, int *pos, int32_t blk[64]);

/* Output formats, as MDEC(1)'s bits 28-27 */
enum { MDEC_DEPTH4 = 0, MDEC_DEPTH8 = 1, MDEC_DEPTH24 = 2, MDEC_DEPTH15 = 3 };

/* bytes of output per macroblock: 16x16 pixels at 15/24 bpp (rows of 16 pixels), 8x8 at 4/8 bpp */
int mdec_macroblock_bytes(int depth);

/* one macroblock (Cr, Cb, Y1..Y4 for colour, one Y for mono) from run-length halfwords to pixels: 15 bpp as
 * 0BBBBBGGGGGRRRRR (bit 15 = set15), 24 bpp as R,G,B bytes, rows of 16 pixels top to bottom; signed_out leaves the
 * values signed (MDEC(1) bit 26). Returns the bytes written, 0 when the data is used up. */
int mdec_macroblock(const MdecTables *t, const uint16_t *src, int n, int *pos, int depth, int signed_out, int set15,
                    uint8_t *out);

/* The Huffman stage (libpress's DecDCTvlc): a BS frame (its 8-byte header, then the bitstream as little-endian
 * halfwords read from bit 15 down) to the MDEC's run-length halfwords. BS versions 1, 2 (DC as 10 bits, +1FFh in
 * place of Cr ends the frame) and 3 (DC as Huffman-coded differences); AC by psx-spx's table, escape 000001 + 16
 * raw bits. Writes at most max halfwords to out (no command word) and stops at the end of the frame or of bs_len.
 * Returns the halfwords written, -1 for a header that isn't BS v1-v3. */
int mdec_vlc(const uint8_t *bs, size_t bs_len, uint16_t *out, int max);

/* the AC code for a bit string, for tests: the MDEC halfword (run << 10 | level, negated when s = 1) and the code's
 * length, decoded from bits (MSB first in the low `avail` bits); 0xFE00 for end of block, -1 for none/escape */
int mdec_ac_lookup(uint32_t bits, int avail, int *len);

#endif
