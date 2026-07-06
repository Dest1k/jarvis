# Матрица VRAM — Gemma 4 runtime

JARVIS OS теперь поддерживает только два активных профиля:

| Профиль | Режим | Цель |
|---|---|---|
| `gemma4-mono` | eager / conservative | стабильный запуск, диагностика, self-heal validation |
| `gemma4-turbo` | CUDA graphs / larger batching | максимальная скорость после прогрева |

Оба профиля используют один мультимодальный Gemma 4 dispatcher. Отдельный GUI/vision
vLLM-инстанс не поднимается; высвобождённая VRAM отдаётся под контекст, KV cache и
резерв рабочего стола.

## RTX 5090 / 32 GiB ориентир

| Потребитель | Mono | Turbo |
|---|---:|---:|
| Gemma 4 NVFP4 weights + runtime overhead | ~17.5 GiB | ~17.5 GiB |
| KV cache fp8 / контекст 32k | ~8.5 GiB | ~8.0 GiB |
| CUDA graphs | 0 GiB | ~1 GiB |
| Audio layer, если включён | ~2 GiB | ~2 GiB |
| Резерв Windows/driver/desktop | ~3.5-4 GiB | ~3 GiB |

## Флаги профилей

`gemma4-mono`:

```env
JARVIS_QWEN_GPU_UTIL=0.82
JARVIS_QWEN_MAX_LEN=32768
JARVIS_QWEN_KV_DTYPE=fp8
JARVIS_QWEN_MAX_NUM_SEQS=8
JARVIS_QWEN_ENFORCE_EAGER=--enforce-eager
```

`gemma4-turbo`:

```env
JARVIS_QWEN_GPU_UTIL=0.80
JARVIS_QWEN_MAX_LEN=32768
JARVIS_QWEN_KV_DTYPE=fp8
JARVIS_QWEN_MAX_NUM_SEQS=16
JARVIS_QWEN_ENFORCE_EAGER=
```

Имена некоторых env-ключей сохранены как совместимые legacy aliases, но активная
модель в обоих профилях — Gemma 4 dispatcher.

## CUDA/UVA hardening

```env
CUDA_VISIBLE_DEVICES=0
CUDA_DEVICE_ORDER=PCI_BUS_ID
CUDA_DISABLE_P2P=1
NCCL_P2P_DISABLE=1
```

Эти флаги обязательны для устойчивости WSL2/Docker на дискретной NVIDIA GPU.

## Тюнинг при OOM

1. Запустить без аудио:

```powershell
python jarvis.py up --profile gemma4-mono --no-audio
```

2. Освободить VRAM:

```powershell
python jarvis.py freevram
```

3. Снизить `JARVIS_QWEN_GPU_UTIL`:

```env
JARVIS_QWEN_GPU_UTIL=0.78
```

4. Снизить контекст:

```env
JARVIS_QWEN_MAX_LEN=24576
```

5. Если turbo не стартует из-за graph capture, вернуться на mono.

## Проверочные команды

```powershell
python smoke_check.py
python jarvis.py up --profile gemma4-mono --no-audio
python jarvis.py status
python jarvis.py diag
```
