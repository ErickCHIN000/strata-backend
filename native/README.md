# Inkling CUDA offload fix

`inkling-cuda-headroom.patch` applies only to the Inkling engine sources at Strata v0.2.19, commit `c6f6427d9de9016fd92d13e29740f65625261df7`. The upstream files are MIT licensed; see `LICENSE.strata`.

The original flags for a VRAM reserve or CPU workers select the modular EP backend. In the tested Windows release, that path also performs attention on the CPU and is far slower than the regular CUDA path. The patch keeps single-GPU FP16/FP8 requests on the optimized CUDA backend and implements its expert cap, requested worker count, and context-aware placement. Dual-GPU and integer-KV requests retain the upstream alternate path.

The patch also:

- Includes the whole requested KV cache and DSpark allocations before placing experts, and rejects a silently reduced context.
- Splits cold expert gate/up and down rows across the CPU pool with a barrier between phases; it keeps the original arithmetic within each output row.
- Initializes shared quantization lookup tables before starting the worker threads.
- Matches the prompt score workspace to the 256-row tiles actually used.
- Checks driver-wide free memory before growing the prompt workspace. On Windows this uses the installed system NVML library, because CUDA's WDDM free-memory count excludes some graphics usage. If more workspace would violate the floor, native GPU attention continues without the larger score matrix.
- Reports expert placement alongside the engine version.

The Python build helper makes an independent local checkout under the managed runtime's `inkling-cuda/source`, checks the pinned revision, checks the patch before applying it, and builds only `strata-inkling`. Its cache identity includes the patch, source revision, and GPU architectures. Builds need existing Git, CUDA, C++ tools, and the Strata venv's build dependencies. Cached binaries do not need the compiler again. A source or patch mismatch requires a rebuild; a different supported Strata release needs a port of this patch.

Neither the original managed source checkout nor signed engine packages are patched. Flash Next keeps its existing executable, model files, tokenizer, and saved profile.

## Numerical check

After building, run the following with the managed Strata venv's Python:

```text
python native/verify_cpu_rows.py <managed-strata-runtime-root>
```

This Windows AVX2 check extracts the original and patched host evaluators from the actual source files, links a small CPU-only verifier, and reads two real experts from each of SQ-20's 24 layers. It compares two activation patterns with 1, 2, 3, 8, 14, and 16 threads: 1,327,104 float output values were bit-identical. This checks the SF4/MX8 CPU row calculation; it is not a claim of full-model equivalence across every prompt or backend.
