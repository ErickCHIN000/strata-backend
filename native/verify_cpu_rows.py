"""Compare the patched parallel CPU rows with upstream on actual SQ-20 experts.

Run with the Strata venv from source/terminal: python <this-file> <runtime-root>.
"""

import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "source/terminal"))
import setup as strata
engine = root / "inkling-cuda/source/engine"
directory = root / "inkling-cuda/verify-rows"
directory.mkdir(exist_ok=True)
upstream = root / "source/engine/src/inkling2/cuda/ink_hybrid.cuh"
patched = engine / "src/inkling2/cuda/ink_hybrid.cuh"
cuda = (engine / "src/inkling2/cuda/ink_cuda.cuh").read_text()
constants = cuda[cuda.index("constexpr int kInkPhysLayers"):cuda.index("__host__ __device__ constexpr float ink_sk_half")]
constants = constants.replace("__host__ __device__", "")
metadata = patched.read_text().split("struct InkHostExpertMeta {", 1)[1].split("namespace host_eval {", 1)[0]


def host_code(path):
    source = path.read_text()
    return source[source.index("namespace host_eval {"):source.index("}  // namespace host_eval") + len("}  // namespace host_eval")]


old = host_code(upstream)
new = host_code(patched).replace("namespace host_eval {", "namespace patched_host_eval {", 1)
program = r'''
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <immintrin.h>
#include <thread>
#include <vector>
#include "strata/artifact/strata_reader.hpp"
#define INK_HOST_AVX2 1
namespace strata::ink {
@CONSTANTS@
struct alignas(16) InkQ8_1_G64 { int8_t qs[64]; float d; float s; };
struct InkHostExpertMeta {@METADATA@
@OLD@
@NEW@
}
int main(int argc, char** argv) {
    if (argc != 2) return 2;
    strata::artifact::StrataFileReader reader;
    std::string error;
    if (!reader.open(argv[1], error)) { std::fprintf(stderr, "%s\n", error.c_str()); return 3; }
    using namespace strata::ink;
    host_eval::e4m3_lut_h(); host_eval::ue8m0_lut_h();
    patched_host_eval::e4m3_lut_h(); patched_host_eval::ue8m0_lut_h();
    std::vector<float> x(kInkHidden), reference(kInkHidden), result(kInkHidden), activation(kInkInter);
    int compared = 0;
    for (int layer = 0; layer < 24; ++layer) for (int expert : {0, 127}) {
        int index = layer * 128 + expert;
        std::vector<uint8_t> body;
        if (!reader.read_expert_body(index, body, error)) return 4;
        const auto& entry = reader.expert_directory()[index];
        InkHostExpertMeta meta{body.data(), entry.body_bytes, entry.dn_offset, entry.gu_code_id, entry.dn_code_id};
        for (int pattern = 0; pattern < 2; ++pattern) {
            for (int i = 0; i < kInkHidden; ++i) x[i] = std::sin(float(i * 17 + pattern * 31) * 0.01f);
            host_eval::eval_expert_f32_h(meta, x.data(), reference.data());
            for (int count : {1, 2, 3, 8, 14, 16}) {
                for (int phase = 0; phase < 2; ++phase) {
                    std::vector<std::thread> threads;
                    for (int worker = 0; worker < count; ++worker) threads.emplace_back([&, worker, phase]() {
                        patched_host_eval::eval_expert_f32_h(meta, x.data(), result.data(), activation.data(), phase, worker, count);
                    });
                    for (auto& thread : threads) thread.join();
                }
                if (std::memcmp(reference.data(), result.data(), kInkHidden * sizeof(float)) != 0) {
                    std::fprintf(stderr, "Mismatch: layer=%d expert=%d pattern=%d threads=%d\n", layer, expert, pattern, count);
                    return 5;
                }
                compared += kInkHidden;
            }
        }
    }
    std::printf("CPU row parity: %d float outputs bit-identical to upstream across 48 SF4/MX8 experts and 1/2/3/8/14/16 threads.\n", compared);
    return 0;
}
'''
for key, value in {"CONSTANTS": constants, "METADATA": metadata, "OLD": old, "NEW": new}.items():
    program = program.replace(f"@{key}@", value)
(directory / "rows.cpp").write_text(program, encoding="utf-8")
build = root / "inkling-cuda/build"
(directory / "CMakeLists.txt").write_text(f'''cmake_minimum_required(VERSION 3.24)
project(inkling_row_parity LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
add_executable(rows rows.cpp)
target_include_directories(rows PRIVATE "{engine.as_posix()}/include")
target_compile_options(rows PRIVATE /O2 /arch:AVX2)
target_link_libraries(rows PRIVATE "{build.as_posix()}/strata_inkling_core.lib" "{build.as_posix()}/strata_zstd_dec.lib")
''', encoding="utf-8")
_, version = strata.find_nvcc()
strata.cmake_build(directory, directory / "build", "rows", [], strata.find_vcvars(version), str(directory / "build.bat"))
weights = next((root / "models").glob("*/strata-engine--Inkling-2-Mini-Strata/Inkling-2-Mini-SQ-20.strata"))
strata.run([str(directory / "build/rows.exe"), str(weights)])
