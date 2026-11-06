#include <stdio.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "input_map.h"

typedef struct { const char *key, *tab, *group, *capability; } SettingRow;

static const SettingRow rows[] = {
    {"FULLSCREEN", "Screen", "Window", NULL},
    {"WINDOW_SIZE", "Screen", "Window", NULL},
    {"VK_PRESENT", "Screen", "Window", NULL},
    {"GPU_INDEX", "Screen", "Window", NULL},
    {"PERF_OVERLAY", "Screen", "Window", NULL},
    {"OUTPUT_RES", "Screen", "Graphics", "resolution"},
    {"RENDER_SCALE", "Screen", "Graphics", "resolution"},
    {"UPSCALER", "Screen", "Graphics", NULL},
    {"SHARPNESS", "Screen", "Graphics", NULL},
    {"ASPECT", "Screen", "Graphics", "aspect"},
    {"FOV", "Screen", "Graphics", "fov"},
    {"COLOR_PRESET", "Screen", "Colour", NULL},
    {"COLOR_BRIGHTNESS", "Screen", "Colour", NULL},
    {"COLOR_CONTRAST", "Screen", "Colour", NULL},
    {"COLOR_SATURATION", "Screen", "Colour", NULL},
    {"COLOR_GAMMA", "Screen", "Colour", NULL},

    {"FPS_LIMIT", "Game", "Frame rate", "fps60"},
    {"REALTIME", "Game", "Frame rate", "realtime"},
    {"CLOTH_REALTIME", "Game", "Frame rate", "cloth"},
    {"HIGHFPS_FIX", "Game", "Frame rate", "highfps"},
    {"LANG", "Game", "Language", NULL},
    {"OPERATION_MODE", "Game", "Language", NULL},
    {"ASSUME_FOCUS", "Game", "Language", NULL},

    {"VOLUME", "Sound", "Audio", NULL},

    {"PAD_LAYOUT", "Controls", "Controller", NULL},
    {"MOTION", "Controls", "Controller", NULL},
    {"RUMBLE_STRENGTH", "Controls", "Controller", NULL},
    {"RUMBLE_CURVE", "Controls", "Controller", NULL},
    {"RUMBLE_TRIGGERS", "Controls", "Controller", NULL},
    {"STICK_DEADZONE", "Controls", "Controller", NULL},
    {"MOUSE_SENS", "Controls", "Controller", NULL},

    {"MENU_LANG", "Menu", "Language", NULL},

    {"ASYNC_PREEMPT", "Advanced", "Scheduling", NULL},
    {"REPLAY_ASYNC", "Advanced", "Scheduling", NULL},
    {"TIMER_1MS", "Advanced", "Scheduling", NULL},
    {"PIPE_WAIT", "Advanced", "Shaders and pipelines", NULL},
    {"PIPE_SKIP", "Advanced", "Shaders and pipelines", NULL},
    {"LIB_PREBUILD", "Advanced", "Shaders and pipelines", NULL},
    {"LIB_PREBUILD_THREADS", "Advanced", "Shaders and pipelines", NULL},
    {"PIPE_PREBUILD", "Advanced", "Shaders and pipelines", NULL},
    {"PREWARM_XLATE", "Advanced", "Shaders and pipelines", NULL},
    {"GPL", "Advanced", "Shaders and pipelines", NULL},
    {"ASTC", "Advanced", "Textures and memory", NULL},
    {"MEMORY", "Advanced", "Textures and memory", NULL},
    {"TEX_ALIAS", "Advanced", "Textures and memory", NULL},
    {"TEX_VRAM_PRESSURE", "Advanced", "Textures and memory", NULL},
    {"ASTC_BC", "Advanced", "Textures and memory", NULL},
    {"LOD_GUARD", "Advanced", "Render fixes", NULL},
    {"LOD_DISTANCE", "Advanced", "Render fixes", NULL},
    {"FIX_C0", "Advanced", "Render fixes", NULL},
    {"FIX_ZERO", "Advanced", "Render fixes", NULL},
    {"BIND_SKIP", "Advanced", "Render fixes", NULL},
    {"SHADER_MEMO", "Advanced", "Render fixes", NULL},
    {"PIPE_MEMO", "Advanced", "Render fixes", NULL},
    {"TEX_MEMO", "Advanced", "Render fixes", NULL},
    {"DESC_TEMPLATE", "Advanced", "Render fixes", NULL},
    {"PUSH_SKIP", "Advanced", "Render fixes", NULL},
    {"SPLIT_DRAWS", "Advanced", "Render fixes", NULL},
    {"VK_VALIDATION", "Advanced", "Vulkan", NULL},
    {"VK_DYN_VERTEX", "Advanced", "Vulkan", NULL},
    {"VK_DYN_A2C", "Advanced", "Vulkan", NULL},
    {"VK_SUBGROUP_PIN", "Advanced", "Vulkan", NULL},
    {"TRACE", "Advanced", "Diagnostics", NULL},
    {"FLIGHT", "Advanced", "Diagnostics", NULL},
    {"FLIGHT_SPIKE_MS", "Advanced", "Diagnostics", NULL},
    {"INPUT_RECORD", "Advanced", "Diagnostics", NULL},
};

static void json_string(const char *s) {
    putchar('"');
    if (s)
        for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
            switch (*p) {
            case '"': fputs("\\\"", stdout); break;
            case '\\': fputs("\\\\", stdout); break;
            case '\b': fputs("\\b", stdout); break;
            case '\f': fputs("\\f", stdout); break;
            case '\n': fputs("\\n", stdout); break;
            case '\r': fputs("\\r", stdout); break;
            case '\t': fputs("\\t", stdout); break;
            default:
                if (*p < 0x20) printf("\\u%04x", *p);
                else putchar(*p);
            }
        }
    putchar('"');
}

static int capability(const char *name) {
    if (!strcmp(name, "highfps")) return rt_patch_set("bounce") >= 0 || rt_patch_set("fade") >= 0;
    if (!strcmp(name, "resolution")) return rt_patch_set("output") >= 0 && rt_patch_set("scene") >= 0;
    return name && rt_patch_set(name) >= 0;
}

static const char *menu_values(const char *key) {
    if (!strcmp(key, "WINDOW_SIZE"))
        return "1280x720|1366x768|1600x900|1920x1080|2560x1440|3200x1800|3840x2160";
    if (!strcmp(key, "OUTPUT_RES"))
        return "1280x720|1600x900|1920x1080|2560x1440|3200x1800|3840x2160|2560x1080|3440x1440|3840x1600|5120x2160|3840x1080|5120x1440|1280x800|1680x1050|1920x1200|2560x1600";
    if (!strcmp(key, "LANG")) return "|en-US|ja|fr|de|it|es|nl|ru|zh-Hans|zh-Hant|ko";
    if (!strcmp(key, "LIB_PREBUILD_THREADS") || !strcmp(key, "PREWARM_XLATE"))
        return "|1|2|4|6|8|12|16|24|32|48|64";
    if (!strcmp(key, "TRACE")) return "|perf";
    // The launcher resolves this menu-only sentinel to logs/input-record.txt beside the executable.
    if (!strcmp(key, "INPUT_RECORD")) return "|@default";
    return NULL;
}

static int menu_values_editable(const char *key) { return !strcmp(key, "WINDOW_SIZE"); }

int settings_schema(void) {
    fputs("{\"settings\":[", stdout);
    for (size_t i = 0; i < sizeof rows / sizeof *rows; i++) {
        const SettingRow *r = &rows[i];
        const CfgMeta *m = cfg_meta(r->key);
        if (!m) {
            fprintf(stderr, "settings-schema: %s has no CfgMeta row\n", r->key);
            return 1;
        }
        if (i) putchar(',');
        fputs("{\"key\":", stdout);
        json_string(m->key);
        fputs(",\"tab\":", stdout);
        json_string(r->tab);
        fputs(",\"group\":", stdout);
        json_string(r->group);
        fputs(",\"section\":", stdout);
        const char *section = cfg_section(r->key);
        if (section) json_string(section);
        else fputs("null", stdout);
        fputs(",\"default\":", stdout);
        json_string(m->def);
        fputs(",\"values\":", stdout);
        if (m->values) json_string(m->values);
        else fputs("null", stdout);
        fputs(",\"menu_values\":", stdout);
        const char *mv = menu_values(r->key);
        if (mv) json_string(mv);
        else fputs("null", stdout);
        printf(",\"menu_values_editable\":%s", menu_values_editable(r->key) ? "true" : "false");
        char var[80];
        snprintf(var, sizeof var, "TOTK_%s", r->key);
        fputs(",\"current\":", stdout);
        json_string(cfg_value(var));
        printf(",\"set\":%s,\"from_env\":%s,\"live\":%s,\"capability\":",
               cfg_is_set(var) ? "true" : "false", cfg_from_env(var) ? "true" : "false",
               m->live ? "true" : "false");
        if (r->capability) json_string(r->capability);
        else fputs("null", stdout);
        printf(",\"supported\":%s}", !r->capability || capability(r->capability) ? "true" : "false");
    }
    fputs("],\"bindings\":[", stdout);
    for (int i = 0; i < ACTION_COUNT; i++) {
        if (i) putchar(',');
        const char *action = input_action_name(i);
        char key[48];
        snprintf(key, sizeof key, "BIND_%s", action ? action : "");
        fputs("{\"action\":", stdout);
        json_string(action);
        fputs(",\"key\":", stdout);
        json_string(key);
        fputs(",\"tab\":\"Controls\",\"section\":\"input\",\"current\":", stdout);
        char var[64];
        snprintf(var, sizeof var, "TOTK_%s", key);
        const char *value = cfg_get(var);
        json_string(value ? value : "");
        printf(",\"set\":%s,\"from_env\":%s,\"live\":true,\"supported\":true,\"stick\":%s}",
               cfg_is_set(var) ? "true" : "false",
               cfg_from_env(var) ? "true" : "false", i >= B_SL_LEFT ? "true" : "false");
    }
    fputs("],\"binding_keys\":[", stdout);
    int first = 1;
    for (int i = KEY_A; i < KEY_COUNT; i++) {
        const char *name = input_key_name(i);
        if (!name) continue;
        if (!first) putchar(',');
        first = 0;
        json_string(name);
    }
    fputs("],\"binding_pads\":[", stdout);
    first = 1;
    for (int i = PAD_SOUTH; i < PAD_COUNT; i++) {
        const char *name = input_pad_name(i);
        if (!name) continue;
        if (!first) putchar(',');
        first = 0;
        json_string(name);
    }
    printf("],\"capabilities\":{\"fps60\":%s,\"realtime\":%s,\"cloth\":%s,\"highfps\":%s,"
           "\"output\":%s,\"scene\":%s,\"aspect\":%s,\"fov\":%s}}\n",
           capability("fps60") ? "true" : "false", capability("realtime") ? "true" : "false",
           capability("cloth") ? "true" : "false", capability("highfps") ? "true" : "false",
           capability("output") ? "true" : "false", capability("scene") ? "true" : "false",
           capability("aspect") ? "true" : "false", capability("fov") ? "true" : "false");
    return ferror(stdout) ? 1 : 0;
}
