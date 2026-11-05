#pragma once
#include <stddef.h>
#include <stdint.h>

// The factor the LOD_DISTANCE setting applies to the voxel LOD switch distances: 1 = untouched.
float lod_override_scale(void);
// 1 for Voxel/VoxelMeshLodParam/*.bgyml.
int lod_override_path(const char *path);
// Scales Lod1Dist and Lod2Dist in a little-endian BYML. Returns the number of fields changed.
size_t lod_override_patch(void *data, size_t size, float scale);
