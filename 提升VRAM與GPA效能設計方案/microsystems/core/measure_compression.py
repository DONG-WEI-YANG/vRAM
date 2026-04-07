"""
真實模型 Per-Layer 壓縮率測量
==============================
下載 safetensors 格式的 LLM 模型，對每個 tensor 用多種壓縮器測量壓縮率。

目的：
  1. 驗證「不同層的壓縮率異質性」是否真實存在
  2. 測量真實數值，取代 benchmark 中的指定值
  3. 找出最佳壓縮策略

用法::

    python -m microsystems.core.measure_compression --model TinyLlama/TinyLlama-1.1B-Chat-v1.0
    python -m microsystems.core.measure_compression --model meta-llama/Llama-3.2-1B
"""

from __future__ import annotations

import csv
import json
import logging
import os
import struct
import time
import zlib
import lzma
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

MB = 1024 * 1024


def _compress_zlib(data: bytes, level: int = 6) -> bytes:
    return zlib.compress(data, level)


def _compress_zlib_fast(data: bytes) -> bytes:
    return zlib.compress(data, 1)


def _compress_lzma(data: bytes) -> bytes:
    return lzma.compress(data, preset=1)  # fast preset


def _compress_zlib_dict(data: bytes, dictionary: bytes) -> bytes:
    """zlib with pre-trained dictionary"""
    c = zlib.compressobj(6, wbytes=-15)  # raw deflate
    c.copy()  # ensure it's valid
    # Use dictionaries via manual prefix
    return zlib.compress(data, 6)  # fallback: dict not directly supported in stdlib


COMPRESSORS_FAST = {
    "zlib_1": _compress_zlib_fast,
    "zlib_6": lambda d: _compress_zlib(d, 6),
}

COMPRESSORS_SLOW = {
    "zlib_9": lambda d: _compress_zlib(d, 9),
}

# 大於 50MB 的 tensor 跳過慢壓縮器
SLOW_THRESHOLD = 50 * MB

COMPRESSORS = {**COMPRESSORS_FAST, **COMPRESSORS_SLOW}


def classify_tensor(name: str) -> str:
    """
    根據 tensor 名稱分類層類型。

    HuggingFace 命名慣例:
      model.layers.{N}.self_attn.{q,k,v,o}_proj.weight  → attention
      model.layers.{N}.mlp.{gate,up,down}_proj.weight    → ffn
      model.embed_tokens.weight                          → embedding
      lm_head.weight                                      → lm_head
      model.layers.{N}.input_layernorm.weight             → norm
    """
    nl = name.lower()
    if "embed" in nl or "wte" in nl or "wpe" in nl:
        return "embedding"
    if "lm_head" in nl:
        return "lm_head"
    # Attention: Llama-style or GPT2-style
    if ("self_attn" in nl or "q_proj" in nl or "k_proj" in nl
            or "v_proj" in nl or "o_proj" in nl
            or ".attn.c_attn" in nl or ".attn.c_proj" in nl):
        return "attention"
    # Attention bias/mask (structural, not learned)
    if ".attn.bias" in nl or ".attn.masked_bias" in nl:
        return "attn_mask"
    # FFN: Llama-style or GPT2-style
    if ("mlp" in nl or "gate_proj" in nl or "up_proj" in nl
            or "down_proj" in nl or "c_fc" in nl):
        return "ffn"
    if "norm" in nl or "layernorm" in nl or "ln_" in nl:
        return "norm"
    if "expert" in nl:
        return "expert_ffn"
    return "other"


def extract_layer_number(name: str) -> int:
    """從 tensor 名稱提取層號"""
    parts = name.split(".")
    for i, p in enumerate(parts):
        if p == "layers" and i + 1 < len(parts):
            try:
                return int(parts[i + 1])
            except ValueError:
                pass
    return -1


def measure_single_tensor(
    tensor_bytes: bytes,
    tensor_name: str,
    shape: tuple,
    dtype: str,
) -> Dict:
    """
    對單一 tensor 測量所有壓縮器的壓縮率。
    """
    original_size = len(tensor_bytes)
    result = {
        "name": tensor_name,
        "layer_num": extract_layer_number(tensor_name),
        "type": classify_tensor(tensor_name),
        "shape": str(shape),
        "dtype": dtype,
        "original_size_bytes": original_size,
        "original_size_mb": round(original_size / MB, 3),
    }

    active = {**COMPRESSORS_FAST}
    if original_size < SLOW_THRESHOLD:
        active.update(COMPRESSORS_SLOW)
    for comp_name, comp_fn in active.items():
        try:
            t0 = time.perf_counter()
            compressed = comp_fn(tensor_bytes)
            t1 = time.perf_counter()

            ratio = original_size / len(compressed)
            throughput_mbs = (original_size / MB) / max(t1 - t0, 1e-9)

            result[f"{comp_name}_size"] = len(compressed)
            result[f"{comp_name}_ratio"] = round(ratio, 4)
            result[f"{comp_name}_throughput_mbs"] = round(throughput_mbs, 1)
        except Exception as e:
            result[f"{comp_name}_ratio"] = 1.0
            result[f"{comp_name}_throughput_mbs"] = 0.0
            logger.warning("Compression %s failed on %s: %s", comp_name, tensor_name, e)

    # 額外統計：tensor 的 entropy 和 pattern 特徵
    arr = np.frombuffer(tensor_bytes, dtype=np.uint8)
    result["byte_entropy"] = round(_byte_entropy(arr), 4)
    result["zero_byte_pct"] = round(np.sum(arr == 0) / len(arr) * 100, 2)

    return result


def _byte_entropy(data: np.ndarray) -> float:
    """計算 byte-level Shannon entropy (bits)"""
    if len(data) == 0:
        return 0.0
    counts = np.bincount(data, minlength=256)
    probs = counts / len(data)
    probs = probs[probs > 0]
    return float(-np.sum(probs * np.log2(probs)))


def measure_model(
    model_id: str,
    output_dir: str,
    max_tensors: int = 0,  # 0 = all
) -> List[Dict]:
    """
    下載模型並測量所有 tensor 的壓縮率。

    Args:
        model_id: HuggingFace model ID (e.g., "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
        output_dir: CSV 輸出目錄
        max_tensors: 限制測量的 tensor 數量 (0 = 全部)
    """
    from huggingface_hub import hf_hub_download, list_repo_files
    from safetensors import safe_open

    os.makedirs(output_dir, exist_ok=True)
    logger.info("Model: %s", model_id)

    # 找到所有 safetensors 檔案
    files = list_repo_files(model_id)
    st_files = [f for f in files if f.endswith(".safetensors")]
    if not st_files:
        raise FileNotFoundError(f"No safetensors files in {model_id}")

    logger.info("Found %d safetensors files: %s", len(st_files), st_files)

    all_results = []
    tensor_count = 0

    for st_file in st_files:
        logger.info("Downloading %s ...", st_file)
        local_path = hf_hub_download(model_id, st_file)

        with safe_open(local_path, framework="np") as f:
            for tensor_name in f.keys():
                if max_tensors > 0 and tensor_count >= max_tensors:
                    break

                try:
                    tensor = f.get_tensor(tensor_name)
                    tensor_bytes = tensor.tobytes()
                    shape = tensor.shape
                    dtype = str(tensor.dtype)
                except (TypeError, ValueError):
                    # Handle unsupported dtypes (e.g., bfloat16)
                    # Read raw bytes via slice
                    tensor_slice = f.get_slice(tensor_name)
                    shape = tuple(tensor_slice.get_shape())
                    dtype = str(tensor_slice.get_dtype())
                    # Calculate byte size from shape and dtype
                    elem_size = {"I32": 4, "I16": 2, "I8": 1, "F32": 4,
                                 "F16": 2, "BF16": 2, "F64": 8, "U8": 1,
                                 "BOOL": 1}.get(dtype, 2)
                    n_elems = 1
                    for d in shape:
                        n_elems *= d
                    total_bytes = n_elems * elem_size
                    # Read via numpy with fallback dtype
                    try:
                        tensor = tensor_slice[:].view(np.uint8)
                        tensor_bytes = tensor.tobytes()
                    except Exception:
                        # Last resort: skip
                        logger.warning("Skipping %s (unsupported dtype %s)", tensor_name, dtype)
                        continue

                if len(tensor_bytes) < 1024:
                    continue

                logger.info(
                    "  [%d] %s  shape=%s  dtype=%s  %.2f MB",
                    tensor_count, tensor_name, shape,
                    dtype, len(tensor_bytes) / MB,
                )

                result = measure_single_tensor(
                    tensor_bytes, tensor_name, shape, dtype,
                )
                all_results.append(result)
                tensor_count += 1

    # ── 寫入 CSV ──
    safe_name = model_id.replace("/", "_")
    csv_path = os.path.join(output_dir, f"compression_{safe_name}.csv")

    if all_results:
        fieldnames = list(all_results[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_results)

    logger.info("Results: %s (%d tensors)", csv_path, len(all_results))

    # ── 印出摘要 ──
    _print_summary(all_results, model_id)

    return all_results


def _print_summary(results: List[Dict], model_id: str) -> None:
    """按層類型印出壓縮率摘要"""
    if not results:
        return

    # 按類型分組
    by_type: Dict[str, List[Dict]] = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r)

    print(f"\n{'═' * 75}")
    print(f"  Compression Ratio Summary: {model_id}")
    print(f"{'═' * 75}")
    print(f"  {'Type':<14} {'Count':>5} {'Size(MB)':>9} {'zlib1':>7} {'zlib6':>7} "
          f"{'zlib9':>7} {'lzma1':>7} {'Entropy':>8} {'Zero%':>6}")
    print(f"  {'─' * 73}")

    for layer_type in ["embedding", "attention", "ffn", "expert_ffn", "norm", "lm_head", "other"]:
        if layer_type not in by_type:
            continue
        items = by_type[layer_type]
        total_mb = sum(r["original_size_mb"] for r in items)
        avg_zlib1 = sum(r.get("zlib_1_ratio", 1) for r in items) / len(items)
        avg_zlib6 = sum(r.get("zlib_6_ratio", 1) for r in items) / len(items)
        avg_zlib9 = sum(r.get("zlib_9_ratio", 1) for r in items) / len(items)
        avg_lzma = sum(r.get("lzma_1_ratio", 1) for r in items) / len(items)
        avg_entropy = sum(r["byte_entropy"] for r in items) / len(items)
        avg_zero = sum(r["zero_byte_pct"] for r in items) / len(items)

        print(f"  {layer_type:<14} {len(items):>5} {total_mb:>8.1f}  "
              f"{avg_zlib1:>6.2f}x {avg_zlib6:>6.2f}x {avg_zlib9:>6.2f}x "
              f"{avg_lzma:>6.2f}x {avg_entropy:>7.3f} {avg_zero:>5.1f}%")

    # 全模型摘要
    total_original = sum(r["original_size_bytes"] for r in results)
    total_zlib6 = sum(r.get("zlib_6_size", r["original_size_bytes"]) for r in results)
    total_zlib1 = sum(r.get("zlib_1_size", r["original_size_bytes"]) for r in results)
    overall_ratio_6 = total_original / max(1, total_zlib6)
    overall_ratio_1 = total_original / max(1, total_zlib1)

    print(f"  {'─' * 73}")
    print(f"  Total: {total_original / MB:.1f} MB → zlib1: {total_zlib1 / MB:.1f} MB "
          f"({overall_ratio_1:.2f}x) | zlib6: {total_zlib6 / MB:.1f} MB ({overall_ratio_6:.2f}x)")
    print(f"{'═' * 75}")

    # ── 按層號排列（論文 Figure 1 用）──
    by_layer: Dict[int, List[Dict]] = {}
    for r in results:
        if r["layer_num"] >= 0:
            by_layer.setdefault(r["layer_num"], []).append(r)

    if by_layer:
        print(f"\n  Per-Layer Detail (zlib6):")
        print(f"  {'Layer':>5} {'Attn':>7} {'FFN':>7} {'Gap':>7}")
        print(f"  {'─' * 30}")
        for layer_num in sorted(by_layer.keys()):
            tensors = by_layer[layer_num]
            attn_r = [r["zlib_6_ratio"] for r in tensors if r["type"] == "attention"]
            ffn_r = [r["zlib_6_ratio"] for r in tensors if r["type"] == "ffn"]
            if attn_r and ffn_r:
                a = sum(attn_r) / len(attn_r)
                f_ = sum(ffn_r) / len(ffn_r)
                gap = a - f_
                print(f"  {layer_num:>5} {a:>6.3f}x {f_:>6.3f}x {gap:>+6.3f}x")


def main():
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Measure per-layer compression ratios")
    parser.add_argument("--model", default="TinyLlama/TinyLlama-1.1B-Chat-v1.0")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-tensors", type=int, default=0)
    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join(
        os.path.dirname(__file__), "..", "benchmark_results",
    )
    measure_model(args.model, output_dir, max_tensors=args.max_tensors)


if __name__ == "__main__":
    main()
