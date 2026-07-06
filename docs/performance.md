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

This lets JARVIS move independent work into durable tasks, offload role briefs to
LAN/Mesh workers when configured, and keep the chat context small.
