/* psx_call_on_stack(fn, top): run fn() on another stack, then come back. */
#if defined(__APPLE__)
#define SYM(x) _##x
#else
#define SYM(x) x
#endif
    .text
    .globl SYM(psx_call_on_stack)
#if defined(__x86_64__)
SYM(psx_call_on_stack):
    pushq %rbp
    movq %rsp, %rbp
    movq %rsi, %rsp
    andq $-16, %rsp
    callq *%rdi
    movq %rbp, %rsp
    popq %rbp
    retq
#elif defined(__aarch64__)
    .p2align 2
SYM(psx_call_on_stack):
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    and x1, x1, #-16
    mov sp, x1
    blr x0
    mov sp, x29
    ldp x29, x30, [sp], #16
    ret
#endif
