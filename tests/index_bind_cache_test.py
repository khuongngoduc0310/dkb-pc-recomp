"""CPU regression for the replay's index-buffer binding cache.

The fixture embeds the production bind_index_buffer, bind_index, restart_for, op_draw_idx and op_draw_idx_ind
unchanged; only the Vulkan command recording, guest-memory resolution and transient scratch buffers are stand-ins.
It checks that the cache skips exactly the repeats of buffer, offset and index type, that a new command buffer or an
external state reset binds again, that byte indices are still widened and primitive restart still applied, that
indirect draws resolve their span after VK_MEM_USE, and that the traffic census and the bind/skip counters follow the
recorded commands. No Vulkan device, driver or game data is used.
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

typedef uint64_t VkBuffer;
typedef uint64_t VkDeviceSize;
typedef uint64_t VkCommandBuffer;
typedef int VkIndexType;
#define VK_INDEX_TYPE_UINT16 0
#define VK_INDEX_TYPE_UINT32 1

enum { NVN_INDEX_TYPE_UNSIGNED_BYTE = 0, NVN_INDEX_TYPE_UNSIGNED_SHORT = 1, NVN_INDEX_TYPE_UNSIGNED_INT = 2 };
enum {
    VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST = 0,
    VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP = 1,
    VK_PRIMITIVE_TOPOLOGY_TRIANGLE_FAN = 2,
    VK_PRIMITIVE_TOPOLOGY_LINE_STRIP = 3,
    VK_PRIMITIVE_TOPOLOGY_LINE_STRIP_WITH_ADJACENCY = 4,
    VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP_WITH_ADJACENCY = 5,
};

enum { VK_MCLS_HOST_UNC = 2, VK_MCLS_SCRATCH = 5 };
enum { VK_MUSE_IB = 1, VK_MUSE_IND = 4, VK_MUSE_CPU_READ = 7 };
enum { W_INDEX, W_INDIRECT, W_RESTART };

typedef struct VkExec VkExec;
typedef struct {
    VkBuffer buffer;
    VkDeviceSize offset, avail;
    void *alloc;
    int cls;
    void *pool;
} VkMemSpan;
typedef struct { int32_t enable; uint32_t index; } p_prim_restart;
typedef struct { int32_t prim, itype, count, base_vertex, base_instance, instances; uint64_t ibuf; } p_draw_idx;
typedef struct { int32_t prim, itype; uint64_t ibuf, ind; } p_draw_idx_ind;
typedef struct {
    uint32_t indexCount, instanceCount, firstIndex;
    int32_t vertexOffset;
    uint32_t firstInstance;
} VkDrawIndexedIndirectCommand;
_Static_assert(sizeof(VkDrawIndexedIndirectCommand) == 20, "indirect command layout");

struct VkExec {
    VkCommandBuffer cb;
    int recorded;
    p_prim_restart prim_restart;
};

typedef struct { uint64_t draws, ib_binds, ib_skips; } VkExecFrame;
static VkExecFrame vk_exec_frame;

/* State the production functions read besides the fixture's Vulkan stand-ins. */
static uint32_t c0_binst __attribute__((unused));
static int vk_mirror_on;
static struct { VkBuffer ib_buf; VkDeviceSize ib_off; VkIndexType ib_type; int ib_valid; } B;

static unsigned checks, failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); } } while (0)

static int warn_key, warnings;
static char warn_text[256];
static void rt_fatal(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    abort();
}
static void warn_once(int key, const char *fmt, ...) {
    va_list ap;
    warnings++, warn_key = key;
    va_start(ap, fmt);
    vsnprintf(warn_text, sizeof warn_text, fmt, ap);
    va_end(ap);
}

static int prepare_ok = 1, prepare_calls;
static int prepare_draw(VkExec *ex, int prim, int32_t count, int restart) {
    (void)ex, (void)prim, (void)count, (void)restart;
    prepare_calls++;
    return prepare_ok;
}

/* The production topology mapping is not part of this test: the fixture passes Vulkan topologies directly. */
static int vk_pipeline_topology(int prim) { return prim; }
static void note_work(const VkExec *ex, int kind, uint64_t work) { (void)ex, (void)kind, (void)work; }
static int vk_sync_noting(void) { return 0; }
static void vk_sync_note_args(uint64_t addr) { (void)addr; }

/* Traffic census and mirror stand-ins: the cache must not change what a draw reports or resolves. */
static uint64_t ib_bytes, cpu_read_bytes, scratch_bytes, ind_bytes, bound_notes;
static void vk_mem_count(int cls, int use, uint64_t bytes) {
    if (cls == VK_MCLS_SCRATCH) scratch_bytes += bytes;
    else if (use == VK_MUSE_IB) ib_bytes += bytes;
    else if (use == VK_MUSE_IND) ind_bytes += bytes;
    else if (use == VK_MUSE_CPU_READ) cpu_read_bytes += bytes;
}
static void vk_mem_bound_note(void *pool) { (void)pool, bound_notes++; }
static void vk_mem_use(VkMemSpan *sp);
#define VK_MEM_USE(sp) do { if (vk_mirror_on && (sp).pool) vk_mem_use(&(sp)); } while (0)
#define VK_MEM_BOUND(sp) do { if ((sp).pool) vk_mem_bound_note((sp).pool); } while (0)

static int resolve_ok = 1, resolve_calls;
static VkMemSpan resolved, indirect_span;
enum { IND_ADDR = 0x7000 };
static int vk_mem_resolve(uint64_t addr, uint64_t size, VkMemSpan *out) {
    resolve_calls++;
    if (!resolve_ok) return 0;
    *out = addr == (uint64_t)IND_ADDR ? indirect_span : resolved;
    out->avail = size;
    return 1;
}

/* Transient chunks: every scratch copy gets a fresh buffer, as the replay's bump allocator does for generated indices. */
#define SCRATCH_MAX 32
static uint8_t scratch_mem[SCRATCH_MAX][4096];
static VkBuffer scratch_buf[SCRATCH_MAX];
static int nscratch;
static uint8_t *scratch(VkExec *ex, uint64_t size, VkBuffer *buf, VkDeviceSize *off) {
    (void)ex;
    if (nscratch == SCRATCH_MAX || size > sizeof scratch_mem[0]) abort();
    scratch_buf[nscratch] = 0x10000 + (VkBuffer)nscratch;
    *buf = scratch_buf[nscratch], *off = 0;
    return scratch_mem[nscratch++];
}

static int binds, draws, indirect_draws, mirror_swaps, mirror_use;
static VkBuffer bind_buffer, mirror_buffer = 0xB2;
static VkDeviceSize bind_offset;
static VkIndexType bind_type;
static const uint8_t *bind_data;
static int mirror_swap;
static void vk_mem_use(VkMemSpan *sp) {
    mirror_use++;
    if (mirror_swap) sp->buffer = mirror_buffer, mirror_swaps++;
}

static void rc_vkCmdBindIndexBuffer(VkCommandBuffer cb, VkBuffer buffer, VkDeviceSize offset, VkIndexType type) {
    CHECK(cb == 1);
    binds++, bind_buffer = buffer, bind_offset = offset, bind_type = type, bind_data = NULL;
    for (int i = 0; i < nscratch; i++)
        if (scratch_buf[i] == buffer) bind_data = scratch_mem[i];
}
static void rc_vkCmdDrawIndexed(VkCommandBuffer cb, uint32_t n, uint32_t inst, uint32_t first, int32_t base, uint32_t binst) {
    (void)cb, (void)n, (void)inst, (void)first, (void)base, (void)binst;
    draws++;
}
static void rc_vkCmdDrawIndexedIndirect(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, uint32_t n, uint32_t s) {
    (void)cb, (void)b, (void)o, (void)n, (void)s;
    indirect_draws++;
}

#define vkCmdBindIndexBuffer rc_vkCmdBindIndexBuffer
#define vkCmdDrawIndexed rc_vkCmdDrawIndexed
#define vkCmdDrawIndexedIndirect rc_vkCmdDrawIndexedIndirect

@PRODUCTION@

static VkExec ex;

static void state(void) {
    memset(&ex, 0, sizeof ex);
    ex.cb = 1;
    vk_exec_draw_new_cb();   /* a new command buffer: no binding is inherited */
    resolve_ok = 1, resolve_calls = 0, warnings = 0, warn_key = -1, prepare_calls = 0;
    vk_mirror_on = 0, mirror_swap = 0;
    resolved = (VkMemSpan){.buffer = 0xB0, .offset = 0x40, .avail = 1 << 20, .cls = VK_MCLS_HOST_UNC, .pool = (void *)0x1};
    indirect_span = (VkMemSpan){.buffer = 0x1A0, .offset = 0, .avail = 1 << 20, .cls = VK_MCLS_HOST_UNC};
}

static void reset_counters(void) {
    binds = draws = indirect_draws = mirror_swaps = mirror_use = 0;
    ib_bytes = cpu_read_bytes = scratch_bytes = ind_bytes = bound_notes = 0;
    bind_buffer = bind_offset = bind_type = 0, bind_data = NULL;
    vk_exec_frame.ib_binds = vk_exec_frame.ib_skips = vk_exec_frame.draws = 0;
}

static void draw_idx(int itype, int count, uint64_t ibuf) {
    p_draw_idx d = {.prim = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST, .itype = itype, .count = count, .instances = 1, .ibuf = ibuf};
    op_draw_idx(&ex, &d);
}

static void draw_idx_ind(int itype, uint64_t ibuf) {
    p_draw_idx_ind d = {.prim = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST, .itype = itype, .ibuf = ibuf, .ind = IND_ADDR};
    op_draw_idx_ind(&ex, &d);
}

static void cache_hits_and_misses(void) {
    state(), reset_counters();
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 1 && vk_exec_frame.ib_binds == 1 && vk_exec_frame.ib_skips == 0);
    CHECK(bind_buffer == 0xB0 && bind_offset == 0x40 && bind_type == VK_INDEX_TYPE_UINT16);
    CHECK(ex.recorded == 2 && vk_exec_frame.draws == 1);   /* the bind and the draw */
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 1 && vk_exec_frame.ib_binds == 1 && vk_exec_frame.ib_skips == 1);
    CHECK(ex.recorded == 3 && vk_exec_frame.draws == 2);   /* only the draw */

    resolved.offset = 0x44;
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 2 && vk_exec_frame.ib_skips == 1);      /* another offset */
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_INT, 6, 0x6000);
    CHECK(binds == 3 && vk_exec_frame.ib_skips == 1);      /* another type */
    CHECK(bind_type == VK_INDEX_TYPE_UINT32);
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_INT, 6, 0x6000);
    CHECK(binds == 3 && vk_exec_frame.ib_skips == 2);
    resolved.buffer = 0xB1;
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_INT, 6, 0x6000);
    CHECK(binds == 4 && vk_exec_frame.ib_skips == 2);      /* another buffer, same offset and type */
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_INT, 6, 0x6000);
    CHECK(binds == 4 && vk_exec_frame.ib_skips == 3);
    CHECK(ex.recorded == 11);
    CHECK(ib_bytes == 3 * 12 + 4 * 24 && bound_notes == 7);   /* the census counts every draw, skipped binds included */
}

static void new_command_buffer_and_reset(void) {
    state(), reset_counters();
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 1 && vk_exec_frame.ib_skips == 1);
    vk_exec_draw_new_cb();                                  /* a split or external invalidation forgets the binding */
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 2 && vk_exec_frame.ib_binds == 2 && vk_exec_frame.ib_skips == 1);
    CHECK(ex.recorded == 5);
}

static void byte_indices_are_widened(void) {
    static const uint8_t src[4] = {0x11, 0xff, 0x22, 0xff};
    state(), reset_counters();
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_BYTE, 4, (uint64_t)(uintptr_t)src);
    CHECK(binds == 1 && vk_exec_frame.ib_binds == 1 && vk_exec_frame.ib_skips == 0);
    CHECK(bind_type == VK_INDEX_TYPE_UINT16 && bind_data != NULL);
    const uint16_t *ix = (const uint16_t *)bind_data;
    CHECK(ix[0] == 0x11 && ix[1] == 0xff && ix[2] == 0x22 && ix[3] == 0xff);
    CHECK(cpu_read_bytes == 4 && scratch_bytes == 8 && ib_bytes == 0);
    CHECK(draws == 1 && ex.recorded == 2);
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_BYTE, 4, (uint64_t)(uintptr_t)src);
    CHECK(binds == 2 && vk_exec_frame.ib_skips == 0);       /* a fresh scratch range: a real bind */
    CHECK(draws == 2 && ex.recorded == 4);

    state(), reset_counters();
    ex.prim_restart = (p_prim_restart){.enable = 1, .index = 0xff};
    p_draw_idx strip = {.prim = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP, .itype = NVN_INDEX_TYPE_UNSIGNED_BYTE,
                        .count = 4, .instances = 1, .ibuf = (uint64_t)(uintptr_t)src};
    op_draw_idx(&ex, &strip);
    ix = (const uint16_t *)bind_data;
    CHECK(bind_type == VK_INDEX_TYPE_UINT16 && warnings == 0);
    CHECK(ix[0] == 0x11 && ix[1] == 0xffff && ix[2] == 0x22 && ix[3] == 0xffff);

    state(), reset_counters();
    resolved.alloc = (void *)1;                             /* byte indices in device-local memory stay unsupported */
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_BYTE, 4, (uint64_t)(uintptr_t)src);
    CHECK(binds == 0 && draws == 0 && warnings == 1 && warn_key == W_INDEX && ex.recorded == 0);
}

static void indirect_binding_cache(void) {
    state(), reset_counters();
    resolved = indirect_span;                               /* the index buffer the indirect draw resolves */
    resolved.pool = (void *)0x1;
    vk_mirror_on = 1;
    draw_idx_ind(NVN_INDEX_TYPE_UNSIGNED_SHORT, 0x6000);
    CHECK(binds == 1 && vk_exec_frame.ib_binds == 1 && indirect_draws == 1 && ex.recorded == 2);
    CHECK(bind_buffer == 0x1A0 && bind_offset == 0 && bind_type == VK_INDEX_TYPE_UINT16);
    draw_idx_ind(NVN_INDEX_TYPE_UNSIGNED_SHORT, 0x6000);
    CHECK(binds == 1 && vk_exec_frame.ib_skips == 1 && indirect_draws == 2 && ex.recorded == 3);
    CHECK(bound_notes == 2 && ind_bytes == 2 * sizeof(VkDrawIndexedIndirectCommand));

    /* A mirror created by VK_MEM_USE changes the span after the address was resolved: the cached handle must not win. */
    mirror_swap = 1, mirror_buffer = 0xB2;
    draw_idx_ind(NVN_INDEX_TYPE_UNSIGNED_SHORT, 0x6000);
    CHECK(mirror_swaps == 1 && bind_buffer == 0xB2 && binds == 2 && vk_exec_frame.ib_skips == 1);
    resolved.buffer = 0xB2;                                 /* the pool keeps its mirror */
    mirror_swap = 0, vk_mirror_on = 0;
    draw_idx_ind(NVN_INDEX_TYPE_UNSIGNED_SHORT, 0x6000);
    CHECK(binds == 2 && vk_exec_frame.ib_skips == 2);

    state(), reset_counters();
    draw_idx_ind(NVN_INDEX_TYPE_UNSIGNED_BYTE, 0x6000);
    CHECK(binds == 0 && indirect_draws == 0 && warnings == 1 && warn_key == W_INDEX);
    state(), reset_counters();
    resolved.offset = 0x21;                                 /* odd offset for 16-bit indices */
    draw_idx_ind(NVN_INDEX_TYPE_UNSIGNED_SHORT, 0x6000);
    CHECK(binds == 0 && indirect_draws == 0 && warnings == 1 && warn_key == W_INDEX);
}

static void validation_and_skips(void) {
    state(), reset_counters();
    draw_idx(3, 6, 0x6000);
    CHECK(binds == 0 && draws == 0 && warnings == 1 && warn_key == W_INDEX && ex.recorded == 0);
    state(), reset_counters();
    resolve_ok = 0;
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 0 && draws == 0 && warnings == 1 && warn_key == W_INDEX);
    state(), reset_counters();
    resolved.offset = 0x41;                                 /* not aligned to 2 bytes */
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 0 && draws == 0 && warnings == 1 && warn_key == W_INDEX);
    state(), reset_counters();
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 0, 0x6000);
    CHECK(binds == 0 && draws == 0 && warnings == 0 && prepare_calls == 0);
    state(), reset_counters();
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);     /* fill the cache */
    prepare_ok = 0;
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);
    CHECK(binds == 1 && draws == 1 && vk_exec_frame.ib_skips == 0 && ex.recorded == 2);
    prepare_ok = 1;
    draw_idx(NVN_INDEX_TYPE_UNSIGNED_SHORT, 6, 0x6000);     /* the skipped draw left the binding alone */
    CHECK(binds == 1 && draws == 2 && vk_exec_frame.ib_skips == 1 && ex.recorded == 3);
}

static void generated_indices(void) {
    state(), reset_counters();
    static const uint32_t quad[6] = {0, 1, 2, 0, 2, 3};
    bind_index(&ex, quad, sizeof quad, VK_INDEX_TYPE_UINT32);
    bind_index(&ex, quad, sizeof quad, VK_INDEX_TYPE_UINT32);
    CHECK(binds == 2 && vk_exec_frame.ib_binds == 2 && vk_exec_frame.ib_skips == 0);
    CHECK(ex.recorded == 2 && scratch_bytes == 0);          /* bind_index counts the scratch copy, not the census */
}

int main(void) {
    cache_hits_and_misses();
    new_command_buffer_and_reset();
    byte_indices_are_widened();
    indirect_binding_cache();
    validation_and_skips();
    generated_indices();
    printf("index-bind-cache: %u checks, %u failures\n", checks, failures);
    return failures != 0;
}
'''


def source(draw):
    code = '\n\n'.join([
        function(draw, 'static void bind_index_buffer('),
        function(draw, 'static void bind_index('),
        function(draw, 'static int restart_for('),
        function(draw, 'static void op_draw_idx('),
        function(draw, 'static void op_draw_idx_ind('),
        function(draw, 'void vk_exec_draw_new_cb('),
    ])
    return FIXTURE.replace('@PRODUCTION@', code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', default=os.environ.get('CC') or shutil.which('clang') or shutil.which('gcc'))
    args = parser.parse_args()
    if not args.cc:
        parser.error('pass --cc with a C compiler')
    draw = (ROOT / 'runtime/vk_exec_draw.c').read_text(encoding='utf-8')
    required = ('static void bind_index_buffer(', 'static void bind_index(', 'static int restart_for(',
                'static void op_draw_idx(', 'static void op_draw_idx_ind(', 'void vk_exec_draw_new_cb(')
    missing = [decl for decl in required if decl not in draw]
    if missing:
        print('vk_exec_draw.c lacks the index-bind cache functions: ' + ', '.join(missing))
        return 1
    with tempfile.TemporaryDirectory(prefix='dkb-index-bind-') as temp:
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
