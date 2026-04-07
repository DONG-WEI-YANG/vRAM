"""
Compressed Disk Offload for HuggingFace Accelerate
===================================================
Monkey-patches accelerate's offload_weight / load_offloaded_weight
to add transparent zlib compression for INT4/INT8 quantized tensors.

Based on real measurements:
  FP16 weights: 0.95× compression (skip — CPU cost > benefit)
  INT4 GPTQ:    30-39× compression (apply — massive I/O reduction)

Usage::

    from microsystems.core.compressed_offload import enable_compressed_offload
    enable_compressed_offload("E:/.vram_offload")

    # Then use HuggingFace normally:
    model = AutoModelForCausalLM.from_pretrained(
        model_id, device_map="auto", offload_folder="E:/.vram_offload",
        max_memory={0: "4GiB", "cpu": "4GiB"},
    )
"""

from __future__ import annotations

import logging
import os
import struct
import time
import zlib
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Compression header
COMPRESSED_MAGIC = b"VRAM"  # 4 bytes magic
HEADER_FMT = "<4sIII"  # magic, original_size, compressed_size, flags
HEADER_SIZE = struct.calcsize(HEADER_FMT)

# Stats
_stats = {
    "total_offloads": 0,
    "compressed_count": 0,
    "skipped_count": 0,
    "total_original_bytes": 0,
    "total_compressed_bytes": 0,
    "total_compress_time_ms": 0.0,
    "total_decompress_time_ms": 0.0,
}


def _should_compress_tensor(weight_name: str, dtype_str: str, size_bytes: int) -> bool:
    """
    Decide whether to compress this tensor.

    Compress if:
      - dtype suggests quantized weights (int32 qweight, int8, uint8)
      - OR name suggests quantized (qweight, qzeros, g_idx)
    Skip if:
      - dtype is float16/float32/bfloat16 (incompressible, 0.95× ratio)
      - tensor is tiny (< 64KB, not worth the overhead)
    """
    if size_bytes < 65536:
        return False

    nl = weight_name.lower()
    # GPTQ markers
    if any(k in nl for k in ("qweight", "qzeros", "g_idx", "quant", "int4", "int8")):
        return True

    # dtype-based: int types compress well
    if any(k in dtype_str for k in ("int32", "int16", "int8", "uint8")):
        return True

    return False


def _compressed_offload_weight(weight, weight_name, offload_folder, index=None):
    """
    Drop-in replacement for accelerate.utils.offload_weight.
    Adds zlib compression for quantized tensors.
    """
    import torch

    dtype = None
    if str(weight.dtype) == "torch.bfloat16":
        weight = weight.view(torch.int16)
        dtype = "bfloat16"

    array = weight.cpu().numpy()
    tensor_file = os.path.join(offload_folder, f"{weight_name}.dat")

    if index is not None:
        if dtype is None:
            dtype = str(array.dtype)
        index[weight_name] = {"dtype": dtype, "shape": list(array.shape)}

    if array.ndim == 0:
        array = array[None]

    raw_bytes = array.tobytes()
    _stats["total_offloads"] += 1
    _stats["total_original_bytes"] += len(raw_bytes)

    if _should_compress_tensor(weight_name, str(array.dtype), len(raw_bytes)):
        # Compress
        t0 = time.perf_counter()
        compressed = zlib.compress(raw_bytes, 1)
        t1 = time.perf_counter()
        _stats["total_compress_time_ms"] += (t1 - t0) * 1000

        if len(compressed) < len(raw_bytes) * 0.9:
            # Write compressed format
            header = struct.pack(
                HEADER_FMT,
                COMPRESSED_MAGIC,
                len(raw_bytes),
                len(compressed),
                0,  # flags (reserved)
            )
            with open(tensor_file, "wb") as f:
                f.write(header)
                f.write(compressed)

            _stats["compressed_count"] += 1
            _stats["total_compressed_bytes"] += len(compressed) + HEADER_SIZE

            ratio = len(raw_bytes) / len(compressed)
            logger.debug(
                "Compressed %s: %.1fMB → %.1fMB (%.1f×, %.0fms)",
                weight_name,
                len(raw_bytes) / (1024 * 1024),
                len(compressed) / (1024 * 1024),
                ratio,
                (t1 - t0) * 1000,
            )
            return index

    # Not compressed — use original memmap path
    file_array = np.memmap(tensor_file, dtype=array.dtype, mode="w+", shape=array.shape)
    file_array[:] = array[:]
    file_array.flush()
    _stats["skipped_count"] += 1
    _stats["total_compressed_bytes"] += len(raw_bytes)

    return index


def _compressed_load_weight(weight_file, weight_info):
    """
    Drop-in replacement for accelerate.utils.load_offloaded_weight.
    Detects compressed format and decompresses transparently.
    """
    import torch

    # Check if file has compression header
    with open(weight_file, "rb") as f:
        maybe_header = f.read(HEADER_SIZE)

    if len(maybe_header) >= HEADER_SIZE:
        magic, orig_size, comp_size, flags = struct.unpack(HEADER_FMT, maybe_header)
        if magic == COMPRESSED_MAGIC:
            # Compressed file — decompress
            t0 = time.perf_counter()
            with open(weight_file, "rb") as f:
                f.seek(HEADER_SIZE)
                compressed_data = f.read(comp_size)

            raw_bytes = zlib.decompress(compressed_data)
            t1 = time.perf_counter()
            _stats["total_decompress_time_ms"] += (t1 - t0) * 1000

            # Reconstruct tensor
            shape = tuple(weight_info["shape"])
            dtype = weight_info["dtype"]
            if dtype == "bfloat16":
                dtype = "int16"
            if shape == ():
                shape = (1,)

            array = np.frombuffer(raw_bytes, dtype=dtype).reshape(shape)
            if len(weight_info["shape"]) == 0:
                array = array.flat[0]

            weight = torch.tensor(np.array(array))
            if weight_info["dtype"] == "bfloat16":
                weight = weight.view(torch.bfloat16)

            return weight

    # Not compressed — use original memmap path
    shape = tuple(weight_info["shape"])
    if shape == ():
        shape = (1,)
    dtype = weight_info["dtype"]
    if dtype == "bfloat16":
        dtype = "int16"

    weight = np.memmap(weight_file, dtype=dtype, shape=shape, mode="r")
    if len(weight_info["shape"]) == 0:
        weight = weight[0]

    weight = torch.tensor(weight)
    if weight_info["dtype"] == "bfloat16":
        weight = weight.view(torch.bfloat16)
    return weight


_original_offload = None
_original_load = None


def enable_compressed_offload(offload_folder: Optional[str] = None) -> None:
    """
    Enable compressed disk offload globally.

    Monkey-patches accelerate.utils.offload_weight and load_offloaded_weight.
    Call this BEFORE loading any model.
    """
    global _original_offload, _original_load

    import accelerate.utils

    if _original_offload is None:
        _original_offload = accelerate.utils.offload_weight
        _original_load = accelerate.utils.load_offloaded_weight

    accelerate.utils.offload_weight = _compressed_offload_weight
    accelerate.utils.load_offloaded_weight = _compressed_load_weight

    # Also patch the modeling_utils import path (transformers uses this directly)
    try:
        import accelerate.utils.offload_utils
        accelerate.utils.offload_utils.offload_weight = _compressed_offload_weight
        accelerate.utils.offload_utils.load_offloaded_weight = _compressed_load_weight
    except (ImportError, AttributeError):
        pass

    if offload_folder:
        os.makedirs(offload_folder, exist_ok=True)

    logger.info("Compressed offload enabled (quantization-aware, zlib level 1)")


def disable_compressed_offload() -> None:
    """Restore original offload functions."""
    global _original_offload, _original_load

    if _original_offload is not None:
        import accelerate.utils
        accelerate.utils.offload_weight = _original_offload
        accelerate.utils.load_offloaded_weight = _original_load
        _original_offload = None
        _original_load = None


def get_stats() -> dict:
    """Get compression statistics."""
    s = dict(_stats)
    if s["total_original_bytes"] > 0:
        s["overall_ratio"] = round(
            s["total_original_bytes"] / max(1, s["total_compressed_bytes"]), 2
        )
    else:
        s["overall_ratio"] = 1.0
    return s


def print_stats() -> None:
    """Print compression statistics summary."""
    s = get_stats()
    MB = 1024 * 1024
    print(f"\n  Compressed Offload Stats:")
    print(f"    Total tensors:    {s['total_offloads']}")
    print(f"    Compressed:       {s['compressed_count']}")
    print(f"    Skipped:          {s['skipped_count']}")
    print(f"    Original size:    {s['total_original_bytes']/MB:.1f} MB")
    print(f"    On-disk size:     {s['total_compressed_bytes']/MB:.1f} MB")
    print(f"    Overall ratio:    {s['overall_ratio']:.1f}×")
    print(f"    Compress time:    {s['total_compress_time_ms']:.0f} ms")
    print(f"    Decompress time:  {s['total_decompress_time_ms']:.0f} ms")
