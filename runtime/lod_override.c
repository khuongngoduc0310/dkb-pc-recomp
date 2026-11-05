#include <math.h>
#include <string.h>

#include "cfg.h"
#include "lod_override.h"

#define LOD_DIR "Voxel/VoxelMeshLodParam/"
#define LOD_SUFFIX ".bgyml"

typedef struct { uint8_t *p; size_t n; uint32_t strings; } LodByml;

float lod_override_scale(void) {
    const char *value = cfg_value("TOTK_LOD_DISTANCE");
    if (value && !strcmp(value, "half")) return 0.5f;
    if (value && !strcmp(value, "quarter")) return 0.25f;
    return 1.0f;
}

int lod_override_path(const char *path) {
    size_t dir = sizeof LOD_DIR - 1, suffix = sizeof LOD_SUFFIX - 1;
    if (!path || strncmp(path, LOD_DIR, dir)) return 0;
    const char *name = path + dir;
    size_t n = strlen(name);
    return n > suffix && !strchr(name, '/') && !strcmp(name + n - suffix, LOD_SUFFIX);
}

static int u32_at(const LodByml *b, size_t off, uint32_t *value) {
    if (off + 4 > b->n) return 0;
    memcpy(value, b->p + off, 4);
    return 1;
}

static const char *key_name(const LodByml *b, uint32_t k) {
    uint32_t head, at;
    if (!u32_at(b, b->strings, &head) || (head & 0xff) != 0xc2 || k >= head >> 8 ||
        !u32_at(b, b->strings + 4 + 4 * (size_t)k, &at) || b->strings + at >= b->n ||
        !memchr(b->p + b->strings + at, 0, b->n - b->strings - at))
        return NULL;
    return (const char *)b->p + b->strings + at;
}

static size_t field(const LodByml *b, uint32_t node, const char *name, uint8_t type) {
    uint32_t head;
    if (!u32_at(b, node, &head) || (head & 0xff) != 0xc1) return 0;
    size_t count = head >> 8;
    if (node + 4 + count * 8 > b->n) return 0;
    for (size_t i = 0; i < count; i++) {
        uint32_t e;
        if (!u32_at(b, node + 4 + i * 8, &e)) return 0;
        const char *k = key_name(b, e & 0xffffff);
        if (k && !strcmp(k, name))
            return e >> 24 == type ? node + 8 + i * 8 : 0;
    }
    return 0;
}

size_t lod_override_patch(void *data, size_t size, float scale) {
    if (!data || size < 16 || !(scale > 0) || scale == 1.0f || memcmp(data, "YB", 2)) return 0;
    uint16_t version;
    memcpy(&version, (uint8_t *)data + 2, sizeof version);
    if (version < 2 || version > 7) return 0;

    LodByml b = {data, size, 0};
    uint32_t root;
    if (!u32_at(&b, 4, &b.strings) || !u32_at(&b, 12, &root)) return 0;

    static const char *const names[] = {"Lod1Dist", "Lod2Dist"};
    size_t changed = 0;
    for (size_t i = 0; i < sizeof names / sizeof *names; i++) {
        size_t at = field(&b, root, names[i], 0xd2);
        if (!at || at + 4 > size) continue;
        float value;
        memcpy(&value, b.p + at, sizeof value);
        if (!isfinite(value)) continue;
        value *= scale;
        memcpy(b.p + at, &value, sizeof value);
        changed++;
    }
    return changed;
}
