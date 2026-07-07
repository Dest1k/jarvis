# JARVIS OS — Gemma 4 performance guide

## Active profiles

Only two runtime profiles are active:

| Profile | Purpose | vLLM mode | First choice |
|---|---|---|---|
| `gemma4-mono` | Stable cold-start and diagnostics | eager | first install, debugging, self-heal validation |
| `gemma4-turbo` | Maximum throughput after warm-up | CUDA graphs | daily use after mono is stable |

Both profiles use one multimodal Gemma 4 dispatcher for conversation, coding,
vision reasoning and GUI intent. No separate GUI model is part of the active
runtime.

Cluster offload is intentionally disabled in the active runtime. Treat LAN/Mesh
workers as roadmap only; keep large work local through mission plans until the
cluster path is explicitly re-enabled and tested.

## Recommended startup path

```powershell
python scripts/smoke_check.py --skip-dashboard
python jarvis.py up --profile gemma4-mono --no-audio
python jarvis.py status
python jarvis.py diag
```

When stable:

```powershell
python jarvis.py up --profile gemma4-turbo --no-audio
```

Then enable audio only after the dispatcher is stable:

```powershell
python jarvis.py up --profile gemma4-turbo
```

## Build cache notes for weak networks

The Dockerfiles use BuildKit cache mounts for package managers: apt metadata and
packages, pip wheels, and npm cache are kept in the local builder cache between
normal rebuilds. The model/runtime data lives in named Docker volumes such as
`jarvis-models`, `jarvis-hf`, and `jarvis-vllm-cache`.

If the connection drops during a build, repeat the same build command without
forcing a cache reset. Avoid clearing the Docker builder cache or named volumes
unless you intentionally want to reclaim disk space and are ready to download
packages or models again.

## VRAM pressure response

1. Run `python jarvis.py freevram`.
2. Start `gemma4-mono --no-audio`.
3. Lower `JARVIS_QWEN_GPU_UTIL` from `0.82` to `0.78`.
4. Lower `JARVIS_QWEN_MAX_LEN` from `32768` to `24576`.
5. Keep `CUDA_DISABLE_P2P=1` and `NCCL_P2P_DISABLE=1` for WSL2/UVA stability.

## Throughput notes

- `gemma4-turbo` enables CUDA graphs by setting the enforce-eager flag empty.
- First turbo start can be slower because vLLM captures/compiles and fills cache.
- `--max-num-batched-tokens 8192` and `JARVIS_QWEN_MAX_NUM_SEQS=16` favor parallel
  sub-agent and mission execution.
- If graph capture fails, return to `gemma4-mono`; it is intentionally conservative.

## Autonomy performance

Use mission plans for large tasks:

```text
JARVIS, оформи это как mission plan: <цель>
JARVIS, выполни следующий runnable шаг mission
```

This lets JARVIS move independent work into durable tasks and keep the chat
context small without enabling experimental cluster routing.
