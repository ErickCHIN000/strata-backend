# Inkling offload and stalled-stream fix: v0.6.2

Test system: Windows, RTX 5090 with 31.8 GiB reported VRAM and 125.6 GiB system RAM. Native source is Strata v0.2.19 (`c6f6427`); the private executable identifies itself as `0.1.65-hermes-cuda4`.

## Cause and changes

The user request reached Strata. The original 768K gaming log showed 15,857 prompt tokens, but only 1,376 had been processed after 193 seconds. Reserve/CPU-worker flags selected the modular EP backend, which ran attention on the CPU. The plugin also forwarded a broken upstream stream without a structured error, and its loaded-engine health requirement could prevent a live server from restarting a stopped child.

The Inkling-only native patch retains optimized CUDA attention and batched GPU prompt processing while offloading expert weights, honors the worker count and expert cap, and budgets the complete KV and drafting allocations before placing experts. CPU work is split by rows with a barrier between gate/up and down projections. Growing prompt workspace uses NVML whole-GPU availability on Windows and falls back to the existing GPU attention kernel when more score memory would violate the floor. CUDA was measured reporting about 3.6 GiB more free memory than the whole-device driver reading; the Windows workspace floor also carries a 1 GiB accounting margin.

The bridge now emits an OpenAI-compatible SSE error and `[DONE]` if the engine stops during a stream. It does not retain a failed turn as a successful warm selection. A healthy HTTP server with an unloaded engine can receive the next request so native restart works. Live prompt progress is read from native batch slots and shown in the Strata tab.

The patch is cached under the managed runtime, uses the existing CUDA/C++ tools, and does not patch the original managed source or signed component packages. The signed Flash Next executable hash was verified unchanged. This fix does not edit Flash Next presets; a concurrent preset edit observed during testing was preserved. The 256K presets were not retuned. Existing 512K/768K preset IDs and names were retained.

## Final presets

All four retain FP8 KV, native vision, and one request slot. DSpark is off to fit the full context and requested headroom. Prompt chunk 1,024 reduces repeated host-to-GPU weight transfers. Floors remain automatic-stop conditions, not memory reserved from games.

| Preset | Context | Expert placement reserve MiB | GPU expert cap GiB | CPU workers | Free RAM floor GiB | Free VRAM floor GiB |
|---|---:|---:|---:|---:|---:|---:|
| Inkling SQ-20 512K FP8 Gaming Experimental | 524288 | 12288 | 4 | 12 | 32 | 8 |
| Inkling SQ-20 512K FP8 Speed Experimental | 524288 | 8192 | Automatic | 14 | 16 | 2 |
| Inkling SQ-20 768K FP8 Gaming Experimental | 786432 | 12288 | 0 | 12 | 32 | 8 |
| Inkling SQ-20 768K FP8 Speed Experimental | 786432 | 8192 | Automatic | 14 | 16 | 2 |

## Stream checks through the Hermes provider

The text input was 14,977 tokens of neutral notes followed by an arithmetic question. Each returned `42` with a normal stream terminator. Timing includes cold profile preparation/loading when switching presets. Generation throughput is the native engine counter, including reasoning tokens, over a short 56-70-token answer; it is not a sustained-load benchmark. Free memory is a post-request snapshot.

| Preset | Decode tok/s | First token seconds | Free RAM GiB | Free VRAM GiB |
|---|---:|---:|---:|---:|
| Inkling SQ-20 512K FP8 Gaming Experimental | 31.8 | 19.73 | 81.19 | 9.36 |
| Inkling SQ-20 512K FP8 Speed Experimental | 50.7 | 14.35 | 86.22 | 3.98 |
| Inkling SQ-20 768K FP8 Gaming Experimental | 19.6 | 23.65 | 77.19 | 9.23 |
| Inkling SQ-20 768K FP8 Speed Experimental | 34.4 | 18.4 | 82.03 | 3.91 |

All four also identified a synthetic image as red on the left and blue on the right, with the same profile memory floors enabled. These image replies took about 2.5-5.1 seconds on the warm servers.

The 768K gaming preset keeps all 3,072 expert bodies in RAM: the complete GPU KV allocation plus the 8 GiB free-VRAM floor leaves little room for expert weights. Its roughly 20 tok/s generation remains a real CPU-offload tradeoff. Speed presets use the separately requested 2 GiB floor; they do not promise the gaming preset's 8 GiB allowance.

## Additional checks

- A 64,887-token prompt completed correctly on the 768K gaming profile with chunk 256 in 181.57 seconds, with a sampled minimum of 9.23 GiB VRAM free. The final chunk 1,024 setting was separately checked on the 14,977-token input above.
- After terminating only the native Inkling child, the next message started a new child and returned the correct answer in 7.99 seconds.
- The real Hermes CLI, using the 768K gaming model and its normal agent prompt (16,485 tokens), completed a brief acknowledgement in 46.32 seconds. Its default model settings were preserved. This client check preceded the final chunk-size increase.
- The CPU row verifier compared 1,327,104 float outputs against the unchanged upstream evaluator across 48 actual SQ-20 SF4/MX8 experts, two activation patterns, and 1/2/3/8/14/16 threads: bit-identical.
- 50 plugin tests pass, including build isolation/cache reuse, unsupported-release refusal, configured driver floor, prompt progress, interrupted SSE, and unloaded-engine forwarding. ESM syntax and disposable-checkout update checks are also part of release verification.

## Limits

The full 512K and 768K KV capacities are allocated and the private engine rejects a silently reduced context, but the largest populated prompt tested here was about 65K. Full-capacity prompt processing, simultaneous gaming, multi-GPU operation, other packs, and every sampling/KV combination have not been validated. Rates vary with prompt, generated text, other applications, and expert placement. The fixes are scoped to Inkling; Flash Next retains its existing settings and signed engine.
