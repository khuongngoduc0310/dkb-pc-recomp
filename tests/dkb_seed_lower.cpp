// Authors two tiny original Ampere compute stages and one native passthrough geometry stage, writes them as a
// DKB_SHADER_DUMP folder, and writes nvn2_lower's synthetic control and Maxwell code for each. The seed adapter test
// (tests/dkb_prepare_test.py) builds program records from the lowered bytes and checks that dkb.exe --seed-dkb-shaders
// reproduces them from the native dump, including two packed GS variants that share one code. No game data, no GPU and
// no translator are involved; the lowering is the production runtime/nvn2_lower.cpp.
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "../runtime/nvn2_lower.cpp"
#include "../runtime/nvn_geometry_control.h"

extern "C" void rt_log(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::vfprintf(stderr, fmt, ap);
    va_end(ap);
    std::fputc('\n', stderr);
}

// The real dump-name hash (gfx_shader.c): FNV-1a over the control (size at +0x30) and the code up to +0x44.
extern "C" uint64_t gfx_shader_nvn2_hash(const uint8_t *ctl, const uint8_t *code) {
    uint32_t magic, version, ctl_size, len, c1_end;
    if (!ctl || !code) return 0;
    std::memcpy(&magic, ctl, 4), std::memcpy(&version, ctl + 4, 4);
    std::memcpy(&ctl_size, ctl + 0x30, 4), std::memcpy(&len, ctl + 0x38, 4), std::memcpy(&c1_end, ctl + 0x44, 4);
    if (magic != 0x98761234u || version != 0xC9u) return 0;
    if (!ctl_size) ctl_size = 0xd8;
    if (c1_end > len && c1_end - len <= 0x10000) len = c1_end;
    uint64_t h = 0xcbf29ce484222325ull;
    for (uint32_t i = 0; i < ctl_size; i++) h = (h ^ ctl[i]) * 0x100000001b3ull;
    for (uint32_t i = 0; i < len; i++) h = (h ^ code[i]) * 0x100000001b3ull;
    return h;
}

using namespace Shader::Ampere;

static void bits(Instruction &i, unsigned at, unsigned n, uint32_t value) {
    for (unsigned b = 0; b < n; b++) {
        const unsigned word = (at + b) / 32, bit = (at + b) % 32;
        i.words[word] = (i.words[word] & ~(1u << bit)) | (((value >> b) & 1u) << bit);
    }
}

static Instruction op(uint32_t opcode, uint32_t predicate = 7) {
    Instruction i{};
    bits(i, 0, 12, opcode), bits(i, 12, 4, predicate);
    return i;
}

static Instruction exit_op(uint32_t predicate = 7, uint32_t secondary = 7) {
    auto i = op(0x94d, predicate);
    bits(i, 87, 4, secondary);
    return i;
}

static std::vector<uint8_t> native_control(uint32_t code_len) {
    std::vector<uint8_t> c(0xd8, 0);
    auto put = [&](size_t at, uint32_t v) { std::memcpy(c.data() + at, &v, 4); };
    put(0x00, 0x98761234u), put(0x04, 0xC9u), put(0x08, 0xBu), put(0x0C, 0x000E0170u);
    put(0x30, 0xd8u), put(0x38, code_len);
    put(0x40, 0), put(0x44, 0);
    put(0x58, 1), put(0x5C, 1), put(0x60, 1);
    put(0x64, 0);
    return c;
}

static std::vector<uint8_t> native_code(const std::vector<Instruction> &code) {
    std::vector<uint8_t> bytes(code.size() * 16);
    for (size_t i = 0; i < code.size(); i++) std::memcpy(bytes.data() + i * 16, code[i].words.data(), 16);
    return bytes;
}

static int write_file(const std::filesystem::path &p, const void *data, size_t n) {
    std::ofstream f(p, std::ios::binary);
    if (!f) return 0;
    f.write(static_cast<const char *>(data), (std::streamsize)n);
    return f.good();
}

static int emit(const std::filesystem::path &dump, const std::filesystem::path &out, const char *name,
                const std::vector<Instruction> &code) {
    auto ctl = native_control(static_cast<uint32_t>(code.size() * 16));
    auto bin = native_code(code);
    if (!write_file(dump / (std::string(name) + ".ctl"), ctl.data(), ctl.size()) ||
        !write_file(dump / (std::string(name) + ".bin"), bin.data(), bin.size()))
        return 1;
    const uint8_t *nctl, *ncode;
    if (!nvn2_lower(ctl.data(), bin.data(), &nctl, &ncode)) {
        std::fprintf(stderr, "%s does not lower\n", name);
        return 1;
    }
    uint32_t len;
    std::memcpy(&len, nctl + 0x704, 4);
    if (!write_file(out / (std::string(name) + ".lowered.ctl"), nctl, 0x880) ||
        !write_file(out / (std::string(name) + ".lowered.bin"), ncode, len))
        return 1;
    std::printf("%s: native %zu bytes, lowered %u bytes, control stage %u\n", name, bin.size(), len,
                [&] { uint32_t s; std::memcpy(&s, nctl + 0x714, 4); return s; }());
    return 0;
}

// A native passthrough geometry stage: an SPH the lowering relabels to the portable kind-1/version-3 form, then EXIT.
// The draw path packs TKGSMETA into the synthetic control after lowering; two packed variants with different skip masks
// share the same lowered code, which is exactly the case the seed adapter must verify by code.
static int emit_gs(const std::filesystem::path &dump, const std::filesystem::path &out) {
    std::vector<uint8_t> bin(0x90, 0);
    auto put = [&](size_t at, uint32_t v) { std::memcpy(bin.data() + at, &v, 4); };
    put(0x00, 0x01001081u);   // kind 1, version 4, type 4, bit 24 = passthrough
    put(0x08, 0x01000000u);   // output topology byte the packed GS contract reads
    put(0x0c, 0x07000000u);   // output primitive
    put(0x10, 1);             // max output vertices
    Instruction e = exit_op();
    std::memcpy(bin.data() + 0x80, e.words.data(), 16);
    auto ctl = native_control(static_cast<uint32_t>(bin.size()));
    if (!write_file(dump / "gs.ctl", ctl.data(), ctl.size()) || !write_file(dump / "gs.bin", bin.data(), bin.size()))
        return 1;
    const uint8_t *nctl, *ncode;
    if (!nvn2_lower(ctl.data(), bin.data(), &nctl, &ncode)) {
        std::fprintf(stderr, "gs does not lower\n");
        return 1;
    }
    uint32_t len;
    std::memcpy(&len, nctl + 0x704, 4);
    std::vector<uint8_t> code(ncode, ncode + len);
    uint32_t skip1[8] = {0x0fffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu,
                         0xffffffffu};
    uint32_t skip2[8] = {0x0fffffffu, 0xfffffff0u, 0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu,
                         0xffffffffu};
    uint8_t p1[0x880], p2[0x880];
    std::memcpy(p1, nctl, 0x880), std::memcpy(p2, nctl, 0x880);
    if (!nvn_geometry_control_pack_lowered(p1, sizeof p1, ncode, len, skip1) ||
        !nvn_geometry_control_pack_lowered(p2, sizeof p2, ncode, len, skip2)) {
        std::fprintf(stderr, "gs packed control rejected (lowered %u bytes)\n", len);
        return 1;
    }
    if (!write_file(out / "gs.packed1.ctl", p1, sizeof p1) || !write_file(out / "gs.packed2.ctl", p2, sizeof p2) ||
        !write_file(out / "gs.lowered.bin", code.data(), code.size()))
        return 1;
    std::printf("gs: native %zu bytes, lowered %u bytes, two packed controls\n", bin.size(), len);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <out folder>\n", argv[0]);
        return 2;
    }
    const std::filesystem::path out = argv[1];
    const auto dump = out / "dump";
    std::filesystem::create_directories(dump);
    // A: one EXIT. B: a MOV and an EXIT, which lowers to different Maxwell code; the adapter must count it unmatched.
    if (emit(dump, out, "a", {exit_op()})) return 1;
    if (emit(dump, out, "b", {op(0x918), exit_op()})) return 1;
    if (emit_gs(dump, out)) return 1;
    return 0;
}
