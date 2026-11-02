# Local runtime tuning

Measured on 2026-11-02 with an RTX 5080 Laptop GPU (16,303 MiB), Intel Ultra 9 275HX, approximately 95 GiB RAM, LM Studio's llama.cpp CUDA12 AVX2 runtime 2.64.0, and the installed `qwen3.8-27b` Q4_K_M model. OpenCode remains pinned explicitly to this local model.

## Selected profile

The saved [profile](tuning/rtx5080-16k.json) retains **16,384 context tokens**, one inference slot, and the original model weights. It uses Flash Attention, Q8_0 K/V cache, MTP speculative decoding with three draft tokens, 52 GPU layers, eight CPU generation threads, and a CPU vision projector. Moving the projector frees GPU space for text inference; image inference will use CPU for that projector. The coding bridge does not send images.

This changes inference settings, rather than training weights. Q8 cache changes cache precision; the checks below are limited quality evidence, not proof of equivalent behavior on all prompts. The fixed 52-layer placement is specific to this machine. It overrides automatic layer placement; the loader checks for a 16GB NVIDIA GPU and at least 1 GiB remaining VRAM after loading. GPU use by other applications can require automatic placement.

## Measured comparison

All runs used temperature zero, disabled reasoning, streaming responses, context 16,384, and concurrency one. The reference is a fresh automatic-placement load with F16 cache, GPU vision projector, and MTP draft length three; automatic placement varied between 41 and 42 layers during calibration. The selected timings are the mean of two fresh-load tuned runs. Request wall time includes prompt processing and output; model loading is excluded.

| Synthetic case | Reference seconds | Tuned seconds | Lower request latency |
|---|---:|---:|---:|
| 160-token generation | 12.00 | 8.49 | 29.2% |
| 193-token Python function | 17.53 | 12.54 | 28.5% |
| Retrieve markers from 12,626 input tokens | 26.61 | 18.97 | 28.7% |
| Structured function call | 4.42 | 3.02 | 31.6% |

Python generation throughput increased from **11.70 to 16.29 tokens/second**; short generation increased from **14.58 to 21.06 tokens/second**. These are approximate streaming throughput measurements, computed as `(output_tokens - 1) / (elapsed - first_token_time)`. The 160-token case intentionally ends at its token limit and is a speed probe.

The generated Python passed independent assertions for ordering, overlap/endpoint merging, empty input, and input preservation. Both tuned runs retrieved the exact two long-context markers and returned the correct tool name and JSON arguments. Generated code and raw timings are retained in [tuning/results](tuning/results). Calibration profiles are included for comparison. These small synthetic timings are not a throughput guarantee for real repositories, concurrent applications, or battery operation. The full ten-task pilot predates this tuning; its original results are in [VALIDATION.md](VALIDATION.md).

The SDK's requested one-token draft setting was ineffective on this installed server: both its native model API and the backend command line showed three draft tokens. Accordingly, the selected profile explicitly specifies three, matching all measured final runs. The `q8-text52-mtp1` calibration filename denotes the attempted request, not an observed change in draft length. No speed gain is attributed to that request. The loader now checks effective speculative settings through the native API and fails if the server ignores them.

A real OpenCode/MCP smoke task also passed under the selected profile: the local worker repaired email normalization, the bridge independently ran its immutable unittest oracle, and Codex inspected the full patch/source and verification evidence at both review gates. Integration reran the check successfully. It used one attempt, three tool calls, zero repairs, and 45.83 seconds of local execution including integration; both explicit Codex decisions and synthetic code/check evidence are in [bridge-smoke.json](tuning/results/bridge-smoke.json). The original fixture stayed clean. The 55 bridge tests also passed after the portability change.

## Installed alternatives

LM Studio has two language models: Qwen 27B (17.39 GB on disk) and Qwen Flash Next (75.44 GB, IQ1_M). The other installed model is an embedding model and cannot be a coding worker. Only about 29 GiB of system RAM was free during inspection, with substantial memory compression already in use. Flash Next was not loaded or timed: fully resident CPU/GPU weights would exceed the available memory, and loading it could force paging. This selects the fastest **tested 27B profile**, without claiming a measured ranking against Flash Next. No additional models were downloaded.

## Apply, restore, and repeat

Stop active worker batches before changing the runtime. With LM Studio's server running on loopback port 1234, from the project directory:

```powershell
npm ci --prefix tuning --ignore-scripts
npm run load --prefix tuning
python server.py --doctor
python tuning/benchmark.py my-run --long
```

Use the bridge's Python environment for `server.py`. The benchmark needs only Python's standard library. It executes the synthetic model-generated Python in a child process with a five-second limit and current-user access. Benchmark files go under the ignored `tuning/.runtime-backups/benchmarks/` directory. Code, tool arguments, actual token counts, first-token time, completion status, and errors are recorded. An unavailable/incompatible loaded model or a failed correctness check returns a failure exit status.

The loader refuses a busy model or another loaded local language model, does not download models, and resolves only the local device. It backs up the previous load configuration under ignored `tuning/.runtime-backups/`. Restore a specific backup using:

```powershell
node tuning/load-profile.mjs --restore 'tuning/.runtime-backups/<timestamp>.json'
```

For other hardware or greater GPU headroom, use LM Studio's automatic layer placement with Q8 cache and the same 16K context:

```powershell
npm run load:auto --prefix tuning
```

This alternative leaves layer/thread/projector placement to LM Studio and is not the fixed 52-layer benchmark profile. Loading failure attempts to restore the previous loaded configuration. Backup restoration and busy-model refusal were exercised locally. The tuned profile was loaded successfully through the shipped loader, leaving approximately 1.3 GiB GPU headroom at that time. Reapply after restarting/unloading LM Studio; global application defaults are preserved. The bridge itself still requires an already loaded model and never performs cloud fallback.

## Runtime references

- [LM Studio SDK loading](https://lmstudio.ai/docs/typescript/manage-models/loading)
- [Official load configuration definitions](https://github.com/lmstudio-ai/lmstudio-js/blob/main/packages/lms-shared-types/src/llm/LLMLoadModelConfig.ts)
- [LM Studio speculative decoding](https://lmstudio.ai/docs/app/advanced/speculative-decoding)
