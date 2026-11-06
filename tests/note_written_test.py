"""CPU regression for the replay's written-range list.

The actual note_written, its hash and grow run against a plain list that searches every entry, the behaviour the table
replaced. Each call must give the same answer as that list: every distinct address and size once, in first-write order,
with one vk_mem_gpu_pending for each. No Vulkan, guest memory or driver is used.
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

typedef struct VkExec VkExec;
typedef struct { uint64_t addr, size; } Range;
typedef struct {
    Range *written;
    int nwritten, cwritten;
    uint32_t *written_slot;
    uint32_t written_slots;
} CbRes;

static CbRes res;
static Range *pending;
static int npending, cpending;
static unsigned failures;

static void rt_fatal(const char *fmt, ...) { (void)fmt; abort(); }
static CbRes *cb_res(VkExec *ex) { (void)ex; return &res; }
@GROW@
static void vk_mem_gpu_pending(uint64_t addr, uint64_t size) {
    if (npending == cpending) pending = grow(pending, &cpending, sizeof *pending);
    pending[npending++] = (Range){addr, size};
}
@NOTE@

static Range *model;
static int nmodel, cmodel;

static void note_model(uint64_t addr, uint64_t size) {
    for (int i = 0; i < nmodel; i++)
        if (model[i].addr == addr && model[i].size == size) return;
    if (nmodel == cmodel) model = grow(model, &cmodel, sizeof *model);
    model[nmodel++] = (Range){addr, size};
}

static void note(uint64_t addr, uint64_t size) {
    note_written(NULL, addr, size);
    note_model(addr, size);
}

static void check(const char *name) {
    int same = res.nwritten == nmodel && npending == nmodel &&
               !memcmp(res.written, model, sizeof *model * (size_t)nmodel) &&
               !memcmp(pending, model, sizeof *model * (size_t)nmodel);
    int table = res.written_slots && !(res.written_slots & (res.written_slots - 1)) &&
                (uint32_t)res.nwritten < res.written_slots;
    if (!same || !table) failures++, fprintf(stderr, "FAIL %s: %d ranges, %d in the model\n", name, res.nwritten, nmodel);
}

static void reset(void) {
    free(res.written), free(res.written_slot), free(pending), free(model);
    memset(&res, 0, sizeof res);
    pending = NULL, npending = 0, cpending = 0;
    model = NULL, nmodel = 0, cmodel = 0;
}

static uint64_t rng = 0x243f6a8885a308d3ull;
static uint64_t next(void) {
    rng ^= rng << 13, rng ^= rng >> 7, rng ^= rng << 17;
    return rng;
}

int main(void) {
    note(0x1000, 0x40), note(0x1000, 0x40);
    check("a repeated range counts once");
    note(0x1000, 0x80), note(0x2000, 0x40), note(0x1000, 0x40);
    check("address and size each tell ranges apart");
    note(0, 0), note(0, 0), note(UINT64_MAX, UINT64_MAX), note(UINT64_MAX, UINT64_MAX);
    check("the extreme values are ordinary keys");
    reset();

    // Around each table growth, then every entry again: an entry lost while the table was rebuilt would be added twice.
    for (uint64_t i = 0; i < 5000; i++) {
        note(0x100000 + i * 0x100, 0x100);
        check("sequential ranges");
    }
    for (uint64_t i = 0; i < 5000; i++) note(0x100000 + i * 0x100, 0x100);
    check("all sequential ranges again");
    reset();

    // Ranges that repeat as a replay's do, with the same address under a few sizes.
    for (int i = 0; i < 40000; i++) {
        uint64_t r = next();
        note(0x80000000ull + (r % 3000) * 0x40, 0x40ull << (r >> 60 & 3));
    }
    check("mixed repeating ranges");
    reset();

    // Keys equal in the table's low bits: every one probes past the others.
    for (uint64_t i = 0; i < 2000; i++) note(i << 40, 0x10);
    for (uint64_t i = 0; i < 2000; i++) note(i << 40, 0x10);
    check("high address bits only");
    reset();

    // One field shared by every key, so a probe that lands on a neighbour needs the other field to tell them apart.
    for (uint64_t i = 1; i <= 2000; i++) note(0x1000, i);
    for (uint64_t i = 1; i <= 2000; i++) note(0x1000, i);
    check("one address under many sizes");
    reset();
    for (uint64_t i = 1; i <= 2000; i++) note(i * 0x100, 0x40);
    for (uint64_t i = 1; i <= 2000; i++) note(i * 0x100, 0x40);
    check("many addresses under one size");
    reset();

    printf("note_written: %u failures\n", failures);
    return failures != 0;
}
'''


def source(draw):
    return FIXTURE.replace('@GROW@', function(draw, 'static void *grow(')).replace(
        '@NOTE@', function(draw, 'static uint32_t written_hash(') + '\n' + function(draw, 'static void note_written('))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', default=os.environ.get('CC') or shutil.which('clang') or shutil.which('gcc'))
    args = parser.parse_args()
    if not args.cc:
        parser.error('pass --cc with a C compiler')
    draw = (ROOT / 'runtime/vk_exec_draw.c').read_text(encoding='utf-8')
    if 'free(r->written_slot)' not in function(draw, 'static void cb_done('):
        print('cb_done does not free the table')
        return 1
    with tempfile.TemporaryDirectory() as temp:
        src = Path(temp) / 'fixture.c'
        src.write_text(source(draw), encoding='utf-8')
        for opt in ('-O0', '-O2'):
            exe = Path(temp) / ('fixture' + opt + ('.exe' if os.name == 'nt' else ''))
            compiled = subprocess.run([args.cc, '-std=gnu11', opt, '-Wall', '-Wextra', '-Werror', str(src), '-o', str(exe)],
                                      capture_output=True, text=True, timeout=60)
            if compiled.returncode:
                print(compiled.stdout + compiled.stderr)
                return compiled.returncode
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
            print(opt, run.stdout + run.stderr, end='')
            if run.returncode:
                return run.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
