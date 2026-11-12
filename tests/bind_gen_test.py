"""CPU regression for the bind-generation invalidation rules.

The fixture embeds the production bind helpers and bind-op handlers unchanged; only the replay state they read is
reduced to the arrays they touch. It checks that an unchanged program, UBO, SSBO, texture, image or push constant
leaves bind_gen alone (the fast path in bind_sig may stay valid), while every effective change still advances it, and
that invalid bind ranges change neither the state nor the generation. No Vulkan device or game data is used.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from nvn2_compute_c0_test import function

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NVN2_ROOT_TABLES 8
#define NVN2_ROOT_WORDS 64
#define W_BIND 0

typedef struct { uint64_t serial; } prog_t;
typedef struct { int32_t stage, index; uint64_t addr, size; } p_bind_buf;
typedef struct { const prog_t *prog; int32_t stages; } p_bind_program;
typedef struct { int32_t stage, first, n, pad_; uint64_t src; uint64_t h[32]; } p_bind_handles;
typedef struct { int32_t compute, table, offset, bytes; uint64_t src; uint32_t w[64]; } p_push_const;

typedef struct {
    const prog_t *prog;
    int32_t prog_stages;
    p_bind_buf ubo[6][18], ssbo[6][16];
    uint64_t tex[6][128], img[6][32], samp[6][128];
    uint32_t tex_serial;
    uint32_t push[2][NVN2_ROOT_TABLES][NVN2_ROOT_WORDS];
    uint64_t push_set[2][NVN2_ROOT_TABLES];
} VkExec;

static uint64_t bind_gen = 1;
static unsigned checks, failures, warnings, logs;
static struct { int on; uint64_t bind_ops, bind_same; } BS = {.on = 1};
static int bind_stats_on(void) { return BS.on; }
#define CHECK(c) do { checks++; if (!(c)) { failures++; fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); } } while (0)

static void warn_once(int key, const char *fmt, ...) {
    (void)key, (void)fmt;
    warnings++;
}
static void rt_log(const char *fmt, ...) {
    (void)fmt;
    logs++;
}

@PRODUCTION@

static VkExec ex;

static void state(void) {
    memset(&ex, 0, sizeof ex);
    bind_gen = 1;
    warnings = logs = 0;
    BS.bind_ops = BS.bind_same = 0;
}

static void programs(void) {
    static prog_t a, b;
    p_bind_program p = {&a, 1};
    state();
    op_bind_program(&ex, &p);
    CHECK(bind_gen == 2 && ex.prog == &a && ex.prog_stages == 1);
    op_bind_program(&ex, &p);
    CHECK(bind_gen == 2);                                  /* same pointer and mask */
    p = (p_bind_program){&a, 2};
    op_bind_program(&ex, &p);
    CHECK(bind_gen == 3 && ex.prog_stages == 2);           /* stage mask changed */
    p = (p_bind_program){&b, 2};
    op_bind_program(&ex, &p);
    CHECK(bind_gen == 4 && ex.prog == &b);                 /* program changed */
    p = (p_bind_program){NULL, 0};
    op_bind_program(&ex, &p);
    CHECK(bind_gen == 5 && ex.prog == NULL && ex.prog_stages == 0);
    CHECK(BS.bind_ops == 5 && BS.bind_same == 1);          /* the counter sees the one unchanged bind */
}

static void buffers(void) {
    p_bind_buf p = {0, 0, 0x1000, 0x100};
    state();
    op_bind_ubo(&ex, &p);
    CHECK(bind_gen == 2 && ex.ubo[0][0].addr == 0x1000 && ex.ubo[0][0].size == 0x100);
    op_bind_ubo(&ex, &p);
    CHECK(bind_gen == 2);                                  /* same range and size */
    p.size = 0x200;
    op_bind_ubo(&ex, &p);
    CHECK(bind_gen == 3 && ex.ubo[0][0].size == 0x200);
    p.addr = 0x2000;
    op_bind_ubo(&ex, &p);
    CHECK(bind_gen == 4 && ex.ubo[0][0].addr == 0x2000);
    p = (p_bind_buf){6, 0, 0x3000, 0x100};
    op_bind_ubo(&ex, &p);
    p = (p_bind_buf){0, 18, 0x3000, 0x100};
    op_bind_ubo(&ex, &p);
    CHECK(bind_gen == 4);                                  /* invalid slots change nothing */

    state();
    p = (p_bind_buf){2, 3, 0x4000, 0x80};
    op_bind_ssbo(&ex, &p);
    CHECK(bind_gen == 2 && ex.ssbo[2][3].addr == 0x4000);
    op_bind_ssbo(&ex, &p);
    CHECK(bind_gen == 2);
    p = (p_bind_buf){2, 16, 0x5000, 0x80};                 /* past the table: dropped */
    op_bind_ssbo(&ex, &p);
    CHECK(bind_gen == 2 && logs == 1);
    p = (p_bind_buf){2, 20, 0x5000, 0x80};
    op_bind_ssbo(&ex, &p);
    CHECK(bind_gen == 2 && logs == 2);
    p = (p_bind_buf){-1, 0, 0x5000, 0x80};
    op_bind_ssbo(&ex, &p);
    CHECK(bind_gen == 2);
}

static void handles(void) {
    p_bind_handles p = {.stage = 0, .first = 0, .n = 2, .h = {1, 2}};
    state();
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 2 && ex.tex_serial == 1 && ex.tex[0][0] == 1 && ex.tex[0][1] == 2);
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 2 && ex.tex_serial == 1);            /* same handles: no generation, no memo invalidation */
    p.h[1] = 3;
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 3 && ex.tex_serial == 2 && ex.tex[0][1] == 3);
    p = (p_bind_handles){.stage = 1, .first = 0, .n = 2, .h = {1, 2}};
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 4 && ex.tex_serial == 3);            /* another stage's array */
    p.n = 0;
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 4 && ex.tex_serial == 3);            /* empty range */
    p = (p_bind_handles){.stage = 6, .first = 0, .n = 1, .h = {9}};
    op_bind_handles(&ex, &p, 0);
    p = (p_bind_handles){.stage = 0, .first = 127, .n = 2, .h = {9, 9}};
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 4 && warnings == 2);                 /* invalid ranges change nothing */
    p = (p_bind_handles){.stage = 0, .first = 126, .n = 2, .h = {9, 9}};
    op_bind_handles(&ex, &p, 0);
    CHECK(bind_gen == 5 && ex.tex[0][126] == 9 && ex.tex[0][127] == 9);

    state();
    p = (p_bind_handles){.stage = 0, .first = 30, .n = 2, .h = {4, 5}};
    op_bind_handles(&ex, &p, 1);
    CHECK(bind_gen == 2 && ex.img[0][30] == 4 && ex.img[0][31] == 5);
    op_bind_handles(&ex, &p, 1);
    CHECK(bind_gen == 2);
    p = (p_bind_handles){.stage = 0, .first = 31, .n = 2, .h = {6, 7}};
    op_bind_handles(&ex, &p, 1);
    CHECK(bind_gen == 2 && warnings == 1 && ex.img[0][31] == 5);
}

static void pushes(void) {
    p_push_const p = {.compute = 0, .table = 0, .offset = 0, .bytes = 4};
    p.w[0] = 0;
    state();
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 2 && ex.push_set[0][0] == 1);        /* a newly written zero replaces a synthetic default */
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 2);                                  /* same word, already written */
    p.w[0] = 7;
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 3 && ex.push[0][0][0] == 7);
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 3);
    p = (p_push_const){.compute = 0, .table = 0, .offset = 4, .bytes = 4};
    p.w[0] = 7;
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 4 && ex.push[0][0][1] == 7 && ex.push_set[0][0] == 3);
    p = (p_push_const){.compute = 0, .table = 0, .offset = 8, .bytes = 4};
    p.w[0] = 0;
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 5 && ex.push_set[0][0] == 7);        /* written zero at a new word */
    p = (p_push_const){.compute = 0, .table = 1, .offset = 0, .bytes = 4};
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 5 && logs == 1);                     /* graphics pushes only reach table 0 */
    p = (p_push_const){.compute = 1, .table = 1, .offset = 0, .bytes = 4};
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 6 && ex.push_set[1][1] == 1);        /* compute table 1 */
    p.bytes = 0;
    p.offset = 12;
    op_bind_push(&ex, &p);
    CHECK(bind_gen == 6);                                  /* empty push */
}

int main(void) {
    programs();
    buffers();
    handles();
    pushes();
    printf("bind-gen: %u checks, %u failures\n", checks, failures);
    return failures != 0;
}
'''


def source(draw):
    names = (
        'static int bind_buf_set(', 'static int bind_handles(', 'static int bind_program_set(',
        'static int bind_push_set(', 'static void bind_count(', 'static void op_bind_program(',
        'static void op_bind_ubo(', 'static void op_bind_ssbo(', 'static void op_bind_handles(',
        'static void op_bind_push(',
    )
    return FIXTURE.replace('@PRODUCTION@', '\n\n'.join(function(draw, name) for name in names))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', default=os.environ.get('CC') or shutil.which('clang') or shutil.which('gcc'))
    args = parser.parse_args()
    if not args.cc:
        parser.error('pass --cc with a C compiler')
    draw = (ROOT / 'runtime/vk_exec_draw.c').read_text(encoding='utf-8')
    required = ('static int bind_buf_set(', 'static int bind_handles(', 'static int bind_program_set(',
                'static int bind_push_set(', 'static void bind_count(', 'static void op_bind_program(',
                'static void op_bind_ubo(', 'static void op_bind_ssbo(', 'static void op_bind_handles(',
                'static void op_bind_push(')
    missing = [decl for decl in required if decl not in draw]
    if missing:
        print('vk_exec_draw.c lacks the bind-generation helpers: ' + ', '.join(missing))
        return 1
    with tempfile.TemporaryDirectory(prefix='dkb-bind-gen-') as temp:
        src = Path(temp) / 'fixture.c'
        src.write_text(source(draw), encoding='utf-8')
        for opt in ('-O0', '-O2'):
            exe = Path(temp) / ('fixture' + opt + ('.exe' if os.name == 'nt' else ''))
            compiled = subprocess.run([args.cc, '-std=gnu11', opt, '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)],
                                      capture_output=True, text=True, timeout=60)
            if compiled.returncode:
                print(compiled.stdout + compiled.stderr)
                return compiled.returncode
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
            print(opt, run.stdout + run.stderr, end='')
            if run.returncode:
                return run.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
