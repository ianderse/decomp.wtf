/* psx_trace_thunk: the function-entry trace (cmake -DPSX_TRACE=ON, decomp port --trace). tools/cc_ptr32.py --trace
 * puts an inline-asm call to it at the entry of every game function, with the function's PS1 address in x16 (r10 on
 * x86_64) and its host address in x17 (r11). The thunk calls psx_trace_enter(ps1, fn) with every argument register
 * kept: matched C that calls `func_X()` while func_X takes an argument works on the PS1 because $a0 passes through,
 * and on the host only while nothing touches x0 between the caller's entry and the call. -finstrument-functions put
 * the function's own address in x0 for __cyg_profile_func_enter, so the trace itself broke those calls.
 *
 * Saved: x0-x9 (x8 is the struct-return pointer), x16-x17 (the inline asm's inputs, which LLVM expects unchanged)
 * and q0-q15 in full (the C hook keeps only the low halves of v8-v15). The inline asm declares the rest of the
 * caller-saved registers clobbered (x10-x15, lr, q16-q31, the flags), so the compiler keeps nothing there.
 * x86_64: rax, rcx, rdx, rsi, rdi, r8-r11 and xmm0-xmm7 saved; the call site steps over the red zone first. */
#if defined(__APPLE__)
#define SYM(x) _##x
#else
#define SYM(x) x
#endif
    .text
    .globl SYM(psx_trace_thunk)
#if defined(__aarch64__)
    .p2align 2
SYM(psx_trace_thunk):
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    sub sp, sp, #352
    stp x0, x1, [sp, #0]
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    stp x8, x9, [sp, #64]
    stp x16, x17, [sp, #80]
    stp q0, q1, [sp, #96]
    stp q2, q3, [sp, #128]
    stp q4, q5, [sp, #160]
    stp q6, q7, [sp, #192]
    stp q8, q9, [sp, #224]
    stp q10, q11, [sp, #256]
    stp q12, q13, [sp, #288]
    stp q14, q15, [sp, #320]
    mov w0, w16
    mov x1, x17
    bl SYM(psx_trace_enter)
    ldp q14, q15, [sp, #320]
    ldp q12, q13, [sp, #288]
    ldp q10, q11, [sp, #256]
    ldp q8, q9, [sp, #224]
    ldp q6, q7, [sp, #192]
    ldp q4, q5, [sp, #160]
    ldp q2, q3, [sp, #128]
    ldp q0, q1, [sp, #96]
    ldp x16, x17, [sp, #80]
    ldp x8, x9, [sp, #64]
    ldp x6, x7, [sp, #48]
    ldp x4, x5, [sp, #32]
    ldp x2, x3, [sp, #16]
    ldp x0, x1, [sp, #0]
    add sp, sp, #352
    ldp x29, x30, [sp], #16
    ret
#elif defined(__x86_64__)
SYM(psx_trace_thunk):
    pushq %rbp
    movq %rsp, %rbp
    andq $-16, %rsp
    subq $208, %rsp
    movq %rax, 0(%rsp)
    movq %rcx, 8(%rsp)
    movq %rdx, 16(%rsp)
    movq %rsi, 24(%rsp)
    movq %rdi, 32(%rsp)
    movq %r8, 40(%rsp)
    movq %r9, 48(%rsp)
    movq %r10, 56(%rsp)
    movq %r11, 64(%rsp)
    movdqa %xmm0, 80(%rsp)
    movdqa %xmm1, 96(%rsp)
    movdqa %xmm2, 112(%rsp)
    movdqa %xmm3, 128(%rsp)
    movdqa %xmm4, 144(%rsp)
    movdqa %xmm5, 160(%rsp)
    movdqa %xmm6, 176(%rsp)
    movdqa %xmm7, 192(%rsp)
    movl %r10d, %edi
    movq %r11, %rsi
    call SYM(psx_trace_enter)
    movdqa 192(%rsp), %xmm7
    movdqa 176(%rsp), %xmm6
    movdqa 160(%rsp), %xmm5
    movdqa 144(%rsp), %xmm4
    movdqa 128(%rsp), %xmm3
    movdqa 112(%rsp), %xmm2
    movdqa 96(%rsp), %xmm1
    movdqa 80(%rsp), %xmm0
    movq 64(%rsp), %r11
    movq 56(%rsp), %r10
    movq 48(%rsp), %r9
    movq 40(%rsp), %r8
    movq 32(%rsp), %rdi
    movq 24(%rsp), %rsi
    movq 16(%rsp), %rdx
    movq 8(%rsp), %rcx
    movq 0(%rsp), %rax
    movq %rbp, %rsp
    popq %rbp
    ret
#endif
