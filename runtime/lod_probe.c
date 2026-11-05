#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "lod_override.h"

enum { BYML_SIZE = 0x74, LOD1_DIST = 0x58, LOD2_DIST = 0x68 };

static void u32(uint8_t *p, uint32_t v) { memcpy(p, &v, sizeof v); }
static void f32(uint8_t *p, float v) { memcpy(p, &v, sizeof v); }
static float get_f32(const uint8_t *p) {
    float v;
    memcpy(&v, p, sizeof v);
    return v;
}

static void fixture(uint8_t *p) {
    memset(p, 0, BYML_SIZE);
    memcpy(p, "YB", 2);
    p[2] = 7;
    u32(p + 4, 0x10);
    u32(p + 12, 0x50);
    u32(p + 0x10, 0x000004c2);
    u32(p + 0x14, 0x18);
    u32(p + 0x18, 0x21);
    u32(p + 0x1c, 0x2b);
    u32(p + 0x20, 0x34);
    memcpy(p + 0x28, "Lod1Dist", 9);
    memcpy(p + 0x31, "Lod1Value", 10);
    memcpy(p + 0x3b, "Lod2Dist", 9);
    memcpy(p + 0x44, "Lod2Value", 10);
    u32(p + 0x50, 0x000004c1);
    u32(p + 0x54, 0xd2000000);
    f32(p + LOD1_DIST, 200);
    u32(p + 0x5c, 0xd2000001);
    f32(p + 0x60, 180);
    u32(p + 0x64, 0xd2000002);
    f32(p + LOD2_DIST, 420);
    u32(p + 0x6c, 0xd2000003);
    f32(p + 0x70, 1000);
}

static int untouched(const uint8_t *a, const uint8_t *b) {
    for (size_t i = 0; i < BYML_SIZE; i++)
        if ((i < LOD1_DIST || i >= LOD1_DIST + 4) && (i < LOD2_DIST || i >= LOD2_DIST + 4) && a[i] != b[i])
            return 0;
    return 1;
}

static int patched(float scale, float lod1, float lod2) {
    uint8_t original[BYML_SIZE], copy[BYML_SIZE];
    fixture(original);
    memcpy(copy, original, sizeof copy);
    size_t n = lod_override_patch(copy, sizeof copy, scale);
    return n == 2 && get_f32(copy + LOD1_DIST) == lod1 && get_f32(copy + LOD2_DIST) == lod2 &&
           get_f32(copy + 0x60) == 180 && get_f32(copy + 0x70) == 1000 && untouched(original, copy);
}

int probe_lod(void) {
    int bad = 0;
    if (!lod_override_path("Voxel/VoxelMeshLodParam/Default.al__VoxelMeshLodParam.bgyml") ||
        !lod_override_path("Voxel/VoxelMeshLodParam/StormLevel.al__VoxelMeshLodParam.bgyml") ||
        lod_override_path("Voxel/VoxelMeshLodParam/Default.bgyml.zs") ||
        lod_override_path("Voxel/Other/Default.al__VoxelMeshLodParam.bgyml"))
        bad = 1, puts("probe-lod: FAIL path matching");

    if (!patched(0.5f, 100, 210) || !patched(0.25f, 50, 105)) bad = 1, puts("probe-lod: FAIL scaling");

    uint8_t original[BYML_SIZE], copy[BYML_SIZE];
    fixture(original);
    memcpy(copy, original, sizeof copy);
    if (lod_override_patch(copy, sizeof copy, 1.0f) || memcmp(copy, original, sizeof copy))
        bad = 1, puts("probe-lod: FAIL default changed the data");

    uint8_t zero[BYML_SIZE] = {0};
    if (lod_override_patch(zero, sizeof zero, 0.5f) || lod_override_patch(NULL, sizeof zero, 0.5f) ||
        lod_override_patch(zero, 8, 0.5f))
        bad = 1, puts("probe-lod: FAIL malformed input was accepted");

    if (bad) {
        puts("probe-lod: FAILED");
        return 1;
    }
    puts("probe-lod: passed");
    return 0;
}
