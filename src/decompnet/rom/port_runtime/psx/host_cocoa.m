/* Window and keyboard through AppKit, for an x86_64 build on a Mac whose SDL2 is arm64 only (Homebrew on Apple
 * silicon). The system frameworks are universal, so this links under Rosetta. Same keys as host_sdl.c. */
#import <Cocoa/Cocoa.h>

#include "psx_runtime.h"

static NSWindow *win;
static int quit;
static uint16_t pad_bits;
static uint8_t clear_rgb[3];

@interface PsxView : NSView
@end
@implementation PsxView
- (void)drawRect:(NSRect)rect {
    [[NSColor colorWithCalibratedRed:clear_rgb[0] / 255.0 green:clear_rgb[1] / 255.0 blue:clear_rgb[2] / 255.0
                               alpha:1.0] setFill];
    NSRectFill(rect);
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
    host_frame(0, 0, 0);
    return win != nil;
}

void host_frame(uint8_t r, uint8_t g, uint8_t b) {
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
        clear_rgb[0] = r; clear_rgb[1] = g; clear_rgb[2] = b;
        if (win) [[win contentView] display];
    }
}

uint16_t host_pad(void) { return pad_bits; }
int host_quit(void) { return quit; }
void host_shutdown(void) { if (win) [win close]; }
