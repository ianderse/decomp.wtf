/* psx_call32: every call through one of the game's 32-bit function pointers (a PS1 address) goes here
 * (tools/cc_ptr32.py, case 5). The call site is the C call's own, with its own argument list, plus the PS1 address
 * as LLVM's `nest` argument: x15 on arm64 (r10 on x86_64), a scratch register outside the argument registers that
 * the compiler sets at the call itself, so nothing can reuse it in between.
 *
 * Why: the game's MIPS calls through a function pointer with `jalr`, and argument registers the C doesn't set keep
 * what the caller had. Matched C relies on it: `(*slot)()` or `handler()` byte-identical to code whose callee reads
 * $a0, the caller's object, still in $a0. The port used to call psx_host_fn(addr) first and then the host pointer,
 * so x0 arrived as psx_host_fn's return value, the callee's own address (SLUS-01252's func_800C58EC wrote through
 * its own host address truncated). Here the lookup keeps x0-x7, x8 (the struct-return pointer) and q0-q7 exactly as
 * the call site left them, and the trampoline branches (br, no frame of its own) to the host function, which
 * returns straight to the caller with the stack arguments where the caller put them.
 *
 * arm64 fast path: a direct-mapped cache (psx_call32_cache, filled by psx_call32_lookup in psx/dispatch.c) of
 * {PS1 address, the two code words in RAM there when it was looked up, host function}; psx_function_at's answer
 * depends only on the address and those two words (overlays sharing an address are told apart by them), so a hit
 * with the same words in RAM is the same answer. Only x9-x14 and x16 are touched. A miss (or an empty slot)
 * saves the argument registers and asks psx_call32_lookup in C (psx_host_fn: the log, the error and the exit). */
#if defined(__APPLE__)
#define SYM(x) _##x
#else
#define SYM(x) x
#endif
#define PSX_CALL32_SLOTS 1024 /* psx_runtime.h agrees */
    .text
    .globl SYM(psx_call32)
#if defined(__aarch64__)
    .p2align 2
SYM(psx_call32):
    /* slot = cache + ((addr >> 2) & (SLOTS - 1)) * 24: {u32 ps1, u32 word0, u32 word1, u32 pad, u64 fn} */
    ubfx x9, x15, #2, #10
    mov x10, #24
#if defined(__APPLE__)
    adrp x11, SYM(psx_call32_cache)@PAGE
    add x11, x11, SYM(psx_call32_cache)@PAGEOFF
#else
    adrp x11, SYM(psx_call32_cache)
    add x11, x11, :lo12:SYM(psx_call32_cache)
#endif
    madd x11, x9, x10, x11
    ldr x16, [x11, #16]
    cbz x16, 1f                    /* an empty slot (checked first: its address 0 would match a call through null) */
    ldr w12, [x11]
    cmp w12, w15
    b.ne 1f
    mov x13, #0x400000000          /* PSX_HOST_BASE: the code in RAM at the address, filled slots are all in RAM */
    add x13, x13, w15, uxtw
    ldp w13, w14, [x13]
    ldp w9, w10, [x11, #4]
    cmp w13, w9
    ccmp w14, w10, #0, eq
    b.ne 1f
    br x16
1:  stp x29, x30, [sp, #-16]!
    mov x29, sp
    sub sp, sp, #208
    stp x0, x1, [sp, #0]
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    str x8, [sp, #64]
    stp q0, q1, [sp, #80]
    stp q2, q3, [sp, #112]
    stp q4, q5, [sp, #144]
    stp q6, q7, [sp, #176]
    mov w0, w15
    bl SYM(psx_call32_lookup)      /* the host function; it doesn't return when there is none */
    mov x16, x0
    ldp q6, q7, [sp, #176]
    ldp q4, q5, [sp, #144]
    ldp q2, q3, [sp, #112]
    ldp q0, q1, [sp, #80]
    ldr x8, [sp, #64]
    ldp x6, x7, [sp, #48]
    ldp x4, x5, [sp, #32]
    ldp x2, x3, [sp, #16]
    ldp x0, x1, [sp, #0]
    add sp, sp, #208
    ldp x29, x30, [sp], #16
    br x16
#elif defined(__x86_64__)
/* x86_64: no cache. The argument registers (rdi, rsi, rdx, rcx, r8, r9, rax: a varargs call's vector count, and
 * xmm0-xmm7) kept around psx_call32_lookup, then a jmp to the host function. On entry rsp is 8 mod 16. */
SYM(psx_call32):
    pushq %rbp
    movq %rsp, %rbp
    subq $192, %rsp
    movq %rdi, 0(%rsp)
    movq %rsi, 8(%rsp)
    movq %rdx, 16(%rsp)
    movq %rcx, 24(%rsp)
    movq %r8, 32(%rsp)
    movq %r9, 40(%rsp)
    movq %rax, 48(%rsp)
    movdqa %xmm0, 64(%rsp)
    movdqa %xmm1, 80(%rsp)
    movdqa %xmm2, 96(%rsp)
    movdqa %xmm3, 112(%rsp)
    movdqa %xmm4, 128(%rsp)
    movdqa %xmm5, 144(%rsp)
    movdqa %xmm6, 160(%rsp)
    movdqa %xmm7, 176(%rsp)
    movl %r10d, %edi
    call SYM(psx_call32_lookup)
    movq %rax, %r11
    movdqa 176(%rsp), %xmm7
    movdqa 160(%rsp), %xmm6
    movdqa 144(%rsp), %xmm5
    movdqa 128(%rsp), %xmm4
    movdqa 112(%rsp), %xmm3
    movdqa 96(%rsp), %xmm2
    movdqa 80(%rsp), %xmm1
    movdqa 64(%rsp), %xmm0
    movq 48(%rsp), %rax
    movq 40(%rsp), %r9
    movq 32(%rsp), %r8
    movq 24(%rsp), %rcx
    movq 16(%rsp), %rdx
    movq 8(%rsp), %rsi
    movq 0(%rsp), %rdi
    movq %rbp, %rsp
    popq %rbp
    jmp *%r11
#endif
#if defined(__linux__) && defined(__ELF__)
    .section .note.GNU-stack,"",%progbits
#endif
