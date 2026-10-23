/* Window and keyboard through AppKit, for an x86_64 build on a Mac whose SDL2 is arm64 only (Homebrew on Apple
 * silicon). The system frameworks are universal, so this links under Rosetta. Same keys as host_sdl.c. */
#import <Cocoa/Cocoa.h>

#include "psx_runtime.h"

static NSWindow *win;
static int quit;
static uint16_t pad_bits;
static uint32_t pixels[1024 * 512];
static int pic_w, pic_h;

@interface PsxView : NSView
@end
@implementation PsxView
- (void)drawRect:(NSRect)rect {
    CGContextRef cg = [[NSGraphicsContext currentContext] CGContext];
    [[NSColor blackColor] setFill];
    NSRectFill(rect);
    if (pic_w > 0 && pic_h > 0) { /* the display area, nearest-neighbour over the whole view */
        CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
        CGContextRef bm = CGBitmapContextCreate(pixels, (size_t)pic_w, (size_t)pic_h, 8, (size_t)pic_w * 4, cs,
                                                kCGImageAlphaNoneSkipFirst | kCGBitmapByteOrder32Little);
        CGImageRef img = bm ? CGBitmapContextCreateImage(bm) : NULL;
        if (img) {
            CGContextSetInterpolationQuality(cg, kCGInterpolationNone);
            CGContextDrawImage(cg, NSRectToCGRect([self bounds]), img);
            CGImageRelease(img);
        }
        if (bm) CGContextRelease(bm);
        CGColorSpaceRelease(cs);
    }
}
- (BOOL)acceptsFirstResponder { return YES; }
@end

@interface PsxDelegate : NSObject <NSWindowDelegate>
@end
@implementation PsxDelegate
- (void)windowWillClose:(NSNotification *)n { (void)n; quit = 1; }
@end

static uint16_t key_bit(unsigned short code) {
    switch (code) { /* macOS virtual key codes */
    case 60: return 1 << 0;  /* right shift: select */
    case 36: return 1 << 3;  /* return: start */
    case 126: return 1 << 4; case 124: return 1 << 5; case 125: return 1 << 6; case 123: return 1 << 7;
    case 18: return 1 << 8;  /* 1: L2 */
    case 19: return 1 << 9;  /* 2: R2 */
    case 12: return 1 << 10; /* Q: L1 */
    case 13: return 1 << 11; /* W: R1 */
    case 1: return 1 << 12;  /* S: triangle */
    case 7: return 1 << 13;  /* X: circle */
    case 6: return 1 << 14;  /* Z: cross */
    case 0: return 1 << 15;  /* A: square */
    default: return 0;
    }
}

const char *host_name(void) { return "Cocoa"; }

int host_init(const char *title, int w, int h) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        win = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, w, h)
                                          styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                                            backing:NSBackingStoreBuffered
                                              defer:NO];
        [win setTitle:[NSString stringWithUTF8String:title]];
        [win setContentView:[[PsxView alloc] initWithFrame:NSMakeRect(0, 0, w, h)]];
        [win setDelegate:[[PsxDelegate alloc] init]];
        [win center];
        [win makeKeyAndOrderFront:nil];
        [NSApp finishLaunching];
        [NSApp activateIgnoringOtherApps:YES];
    }
    host_present(NULL, 0, 0, 0, 0, 0);
    return win != nil;
}

void host_present(const uint16_t *vram, int x, int y, int w, int h, int rgb24) {
    if (vram && w > 0 && h > 0) {
        gpu_display_rgba(vram, x, y, w, h, rgb24, pixels);
        pic_w = w;
        pic_h = h;
    } else {
        pic_w = pic_h = 0;
    }
    @autoreleasepool {
        if (win) [[win contentView] display];
    }
}

void host_frame(void) {
    @autoreleasepool {
        NSEvent *e;
        while ((e = [NSApp nextEventMatchingMask:NSEventMaskAny untilDate:[NSDate distantPast]
                                          inMode:NSDefaultRunLoopMode dequeue:YES])) {
            if ([e type] == NSEventTypeKeyDown) { pad_bits |= key_bit([e keyCode]); continue; }
            if ([e type] == NSEventTypeKeyUp) { pad_bits &= (uint16_t)~key_bit([e keyCode]); continue; }
            if ([e type] == NSEventTypeFlagsChanged && [e keyCode] == 60) {
                if ([e modifierFlags] & NSEventModifierFlagShift) pad_bits |= 1; else pad_bits &= (uint16_t)~1;
                continue;
            }
            [NSApp sendEvent:e];
        }
    }
}

uint16_t host_pad(void) { return pad_bits; }
int host_quit(void) { return quit; }
void host_shutdown(void) { if (win) [win close]; }
