# Inkling SQ-20 profile checks

Local checks on an RTX 5090 (31.8 GiB reported VRAM) with 125.6 GiB system RAM, Strata v0.2.19 / strata-inkling 0.1.65. These presets were created through the plugin API for the existing local Inkling download. They are not installed automatically on other machines.

## Presets

| Context | Gaming floors (free RAM / VRAM) | Speed floors (free RAM / VRAM) |
|---|---|---|
| 262,144 | 32 GiB / 8 GiB | 16 GiB / 2 GiB |
| 524,288 (experimental) | 32 GiB / 8 GiB | 16 GiB / 2 GiB |
| 786,432 (experimental) | 32 GiB / 8 GiB | 16 GiB / 2 GiB |

All use FP8 KV and one request slot, with vision enabled. Gaming uses an expert-placement reserve of 12,288 MiB, eight CPU offload threads, and prompt chunk 16. The 256K speed preset uses the regular GPU path with DSpark off and prompt chunk 512; explicit reserve and CPU offload flags are omitted. Higher-context speed presets use reserve 6,144 MiB, 14 CPU offload threads, prompt chunk 16, and DSpark on. Other presets leave DSpark off.

The floors are sampled automatic-stop conditions, not allocations reserved from games or an OS memory cap. Clear a floor in the form to disable that check. They apply only to the opted-in Inkling profile. The pre-existing Flash Next profile and default selection were preserved.

## Short comparison

Same 133-token synthetic prompt, capped at 128 generated tokens; generated tokens can include reasoning. This is a raw throughput check, not an answer-quality test. The context column is the engine-reported capacity; the cache was not populated to 256K, 512K, or 768K. Free memory was sampled after the request. All rows in this comparison had DSpark off.

| Preset | Reported context | Decode tok/s | Prompt tok/s | Free RAM GiB | Free VRAM GiB |
|---|---:|---:|---:|---:|---:|
| Inkling SQ-20 256K FP8 Gaming | 262,144 | 10.1 | 21.9 | 88.98 | 11.25 |
| Inkling SQ-20 256K FP8 Speed | 262,144 | 105.1 | 1725.0 | 92.99 | 5.07 |
| Inkling SQ-20 512K FP8 Gaming Experimental | 524,288 | 9.9 | 22.5 | 89.19 | 11.32 |
| Inkling SQ-20 512K FP8 Speed Experimental | 524,288 | 10.8 | 22.3 | 89.75 | 11.28 |
| Inkling SQ-20 768K FP8 Gaming Experimental | 786,432 | 11.8 | 24.2 | 89.74 | 11.28 |
| Inkling SQ-20 768K FP8 Speed Experimental | 786,432 | 11.7 | 24.1 | 89.82 | 11.29 |

An additional 512K speed comparison with DSpark on measured 12.4 decode tok/s and 5333.7 ms prompt time for the same 133-token input. This setting was retained for the larger speed presets.

Separate completed-answer checks returned `42` for `17 + 25` with the final 256K gaming and 768K DSpark speed presets. Both reported their full configured context and stayed ready afterward. These short arithmetic checks do not validate long-context answer quality.

## Native hybrid limitation

The ordinary CUDA path was much faster. With the default prompt chunk of 512, the native hybrid path crashed on a roughly 1,100-token prompt (Windows access violation 0xC0000005). Chunk 64 instead failed a native CUDA copy with an invalid-argument error. The native dense input buffer is sized for 64 hidden rows but some projections take wider rows. Chunk 16 avoids that mismatch in the checks above; the plugin supplies it when blank and rejects larger hybrid chunks or more than 16 slots for this supported engine. GPU expert caps explicitly select the native loader that honors them.

The larger hybrid contexts are experimental and currently slow: a roughly 1,100-token prompt with chunk 16 reached generation without the earlier crash, but exceeded the 180-second benchmark client deadline. A full 512K/768K prompt, simultaneous gaming, multi-GPU operation, and every KV/drafting combination have not been benchmarked. Do not interpret reported maximum context as proof of useful long-context speed or available memory at full occupancy.
