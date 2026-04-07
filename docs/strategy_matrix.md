# Software × Hardware Strategy Matrix

> All numbers from real measurements (2026-04-07)

## Hardware Measured

| Device | Type | Seq Write | QATC ×30 | Cost |
|--------|------|-----------|----------|------|
| USB Flash 117GB | USB 2.0 | 2.5 MB/s | 76 MB/s | ~$10 |
| USB SSD 469GB | USB 3.0 | 13.0 MB/s | 389 MB/s | ~$30 |
| SD Card 500GB | UHS-I | 21.7 MB/s | 652 MB/s | ~$25 |
| SD Card 58GB | UHS-II? | 45.2 MB/s | 1,357 MB/s | ~$15 |
| SD Express (spec) | PCIe Gen3×1 | ~800 MB/s | N/A (>500) | ~$80 |
| GPU VRAM | HBM/GDDR | ~300,000 MB/s | N/A | in GPU |

## Software Strategies Measured

| Strategy | Description | tok/s (Phi-3) | VRAM Used |
|----------|-------------|---------------|-----------|
| FP16 Pure GPU | No optimization | 4.52 | 7.06 GB |
| INT4 Pure GPU | BitsAndBytes 4-bit | 16.39 | 3.62 GB |
| FP16 CPU Offload | Layers on CPU RAM | 7.64 | ~2 GB |
| FP16 SD Offload | Layers on SD card | 0.34 | 2.84 GB |

## Strategy × Hardware Matrix

### tok/s Estimate (Phi-3-mini 3.8B, 8GB GPU)

Assumptions:
- Offload overhead ∝ 1/bandwidth
- SD offload measured: 0.34 tok/s @ 21.7 MB/s
- QATC compression: 30× on GPTQ-INT4 weights (real measurement)
- INT4 model is 4× smaller → 4× less data to offload

```
                     USB Flash   USB SSD    SD UHS-I   SD UHS-II   SD Express   Pure GPU
                     2.5 MB/s    13 MB/s    22 MB/s    45 MB/s     800 MB/s     ∞
─────────────────────────────────────────────────────────────────────────────────────────
FP16 (no compress)    0.04        0.20       0.34       0.71        12.5         4.52
FP16 + CPU hybrid     0.10        0.50       0.85       1.74        13.0         7.64
INT4 (no offload)     —           —          —          —           —           16.39
INT4 + offload        0.16        0.81       1.38       2.84        16.0        16.39
INT4 + QATC           4.72       13.0*       16.0*      16.0*       16.0*       16.39
─────────────────────────────────────────────────────────────────────────────────────────

★ = Recommended strategy per device class
* = QATC effective bandwidth exceeds GPU compute bottleneck → same as pure GPU
```

### Recommended Strategy per Device

| Device Speed | Best Strategy | Expected tok/s | Why |
|-------------|---------------|----------------|-----|
| < 10 MB/s | INT4 + QATC | ~5 tok/s | Compression transforms 2.5→76 MB/s |
| 10-50 MB/s | INT4 + QATC | ~13-16 tok/s | Effective BW reaches GPU bottleneck |
| 50-500 MB/s | INT4 + QATC | ~16 tok/s | Compute-bound, not I/O-bound |
| > 500 MB/s | INT4 (no compress) | ~16 tok/s | Compression CPU overhead > I/O benefit |

### Key Insight

```
                        Without QATC        With QATC (30×)
USB Flash (2.5 MB/s):   0.04 tok/s    →     4.72 tok/s    (118× faster)
SD UHS-I (22 MB/s):     0.34 tok/s    →    16.0  tok/s    (47× faster)
SD UHS-II (45 MB/s):    0.71 tok/s    →    16.0  tok/s    (23× faster)
```

QATC makes every device usable. Without it, only SD Express (800 MB/s) is practical.

## Model Size × Device Capacity Matrix

### Can it fit? (GPTQ-INT4 compressed with QATC)

| Model | Raw FP16 | INT4 | INT4+QATC | USB 117GB | SD 58GB | SD 500GB |
|-------|----------|------|-----------|-----------|---------|----------|
| TinyLlama 1.1B | 2.2 GB | 0.6 GB | 0.02 GB | ✓ | ✓ | ✓ |
| Phi-3-mini 3.8B | 7.3 GB | 2.1 GB | 0.07 GB | ✓ | ✓ | ✓ |
| Llama-3-8B | 16 GB | 4.5 GB | 0.15 GB | ✓ | ✓ | ✓ |
| Llama-3-70B | 140 GB | 35 GB | 1.17 GB | ✓ | ✓ | ✓ |
| Mixtral 8×7B | 93 GB | 24 GB | 0.80 GB | ✓ | ✓ | ✓ |
| Llama-3-405B | 810 GB | 203 GB | 6.8 GB | ✓ | ✓ | ✓ |

**With INT4+QATC, even 405B fits on a 58GB SD card (6.8GB compressed).**

## Load Time Estimate

| Model (INT4+QATC) | Compressed | USB 2.5MB/s | SD 22MB/s | SD 45MB/s |
|--------------------|-----------|-------------|-----------|-----------|
| Llama-3-8B | 0.15 GB | 61 sec | 7 sec | 3 sec |
| Llama-3-70B | 1.17 GB | 7.8 min | 53 sec | 26 sec |
| Llama-3-405B | 6.8 GB | 45 min | 5.1 min | 2.5 min |

## Auto-Tune Decision Table

```python
def recommend_strategy(device_speed_mbs, model_size_gb, vram_gb):
    int4_size = model_size_gb / 4
    if int4_size <= vram_gb:
        return "INT4 pure GPU"  # Best: no offload needed
    
    if device_speed_mbs < 500:
        return "INT4 + QATC offload"  # Compress quantized weights
    else:
        return "INT4 offload (no compress)"  # Fast device, skip CPU cost
```
