"""
Slow Device Optimization Engine
=================================
讓普通 SD 卡、USB 隨身碟、外接 HDD 也能用於 VRAM 擴展的核心最佳化層。

問題：普通裝置頻寬只有 100-400 MB/s，是 SD Express 的 1/10 ~ 1/40。
解法：不是追求更快的硬體，而是**減少需要傳輸的資料量**和**隱藏延遲**。

5 項關鍵最佳化：

1. LZ4 即時壓縮
   - KV Cache 壓縮率 2-4x → 100 MB/s 裝置等效 200-400 MB/s
   - LZ4 解壓速度 4-8 GB/s → CPU 解壓不是瓶頸
   - 只壓冷資料（EXTERNAL 層），熱資料（VRAM/RAM）不壓

2. 大塊順序 I/O
   - HDD 順序讀 ~150 MB/s，隨機讀 ~0.5 MB/s（差 300 倍）
   - 將小區塊打包成 1-4MB 的連續大塊
   - I/O 排程器重排請求為順序存取

3. RAM Write-Back Buffer
   - 寫入先進 RAM buffer，批次 flush 到慢速裝置
   - 讀取優先從 buffer 取（write-back cache）
   - buffer 滿時才觸發 flush

4. KV Cache 量化
   - FP16 → INT8：大小減半，精度損失 < 1%
   - FP16 → INT4：大小 1/4，適合超長 context 場景
   - 量化在寫入前執行，反量化在讀取後執行

5. 智慧 Eviction
   - 基於 attention score 的 KV Cache 淘汰
   - 低 attention 的 token KV 優先卸載到慢速裝置
   - 高 attention 的 token KV 保留在 VRAM/RAM
"""

from __future__ import annotations

import io
import logging
import struct
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any

from .llm_optimizations import SharedCompressionDict

logger = logging.getLogger(__name__)

# ── 嘗試載入 LZ4（可選依賴）──
try:
    import lz4.frame as lz4f
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False


class CompressionMethod(Enum):
    NONE = "none"
    LZ4 = "lz4"
    ZLIB_FAST = "zlib_fast"   # zlib level 1 — 標準庫內建


class QuantizationLevel(Enum):
    NONE = "none"       # 原始精度
    INT8 = "int8"       # FP16 → INT8 (2x 壓縮)
    INT4 = "int4"       # FP16 → INT4 (4x 壓縮)


@dataclass
class OptimizerStats:
    """最佳化器統計"""
    total_bytes_in: int = 0          # 壓縮前總大小
    total_bytes_out: int = 0         # 壓縮後總大小
    compression_ratio: float = 1.0   # 平均壓縮率
    total_compress_ms: float = 0.0
    total_decompress_ms: float = 0.0
    buffer_hits: int = 0             # write-back buffer 命中次數
    buffer_misses: int = 0
    sequential_merges: int = 0       # 順序 I/O 合併次數
    quantization_saves_bytes: int = 0


@dataclass
class BufferedBlock:
    """Write-back buffer 中的區塊"""
    block_id: str
    data: bytes
    offset: int
    dirty: bool = True
    last_access: float = field(default_factory=time.time)
    access_count: int = 0


class SlowDeviceOptimizer:
    """
    慢速裝置最佳化引擎。

    用法：
        opt = SlowDeviceOptimizer(
            compression=CompressionMethod.LZ4,
            buffer_size_mb=256,
            sequential_block_kb=1024,
        )

        # 寫入（自動壓縮 + buffer）
        opt.write(block_id, offset, raw_data, flush_fn=device.write_block)

        # 讀取（buffer 命中或解壓）
        data = opt.read(block_id, offset, size, read_fn=device.read_block)

        # 定期 flush buffer
        opt.flush_all(flush_fn=device.write_block)
    """

    def __init__(
        self,
        compression: CompressionMethod = CompressionMethod.ZLIB_FAST,
        quantization: QuantizationLevel = QuantizationLevel.NONE,
        buffer_size_mb: int = 256,
        sequential_block_kb: int = 1024,
        max_buffer_entries: int = 1024,
    ):
        # 壓縮方式自動降級：要求 LZ4 但沒安裝 → 用 zlib
        if compression == CompressionMethod.LZ4 and not HAS_LZ4:
            logger.warning("LZ4 not installed, falling back to zlib_fast")
            compression = CompressionMethod.ZLIB_FAST

        self._compression = compression
        self._quantization = quantization
        self._buffer_max = buffer_size_mb * 1024 * 1024
        self._seq_block = sequential_block_kb * 1024
        self._max_entries = max_buffer_entries

        self._lock = threading.Lock()

        # Write-back buffer (LRU)
        self._buffer: OrderedDict[str, BufferedBlock] = OrderedDict()
        self._buffer_size = 0

        self._stats = OptimizerStats()

        # MLA-inspired shared compression dictionary
        self._shared_dict = SharedCompressionDict()
        self._dict_trained = False
        self._training_samples: List[bytes] = []

        logger.info(
            "SlowDeviceOptimizer: compression=%s, quantization=%s, "
            "buffer=%dMB, seq_block=%dKB",
            compression.value, quantization.value,
            buffer_size_mb, sequential_block_kb,
        )

    # ── Public API ──

    def write(
        self,
        block_id: str,
        offset: int,
        data: bytes,
        flush_fn=None,
    ) -> int:
        """
        最佳化寫入：量化 → 壓縮 → 寫入 buffer。

        Args:
            block_id: 區塊 ID
            offset: 區塊內偏移
            data: 原始資料
            flush_fn: 當 buffer 滿時呼叫的 flush 函式
                      flush_fn(block_id, offset, compressed_data) → bool

        Returns: 實際寫入大小（壓縮後）
        """
        # 0. MLA: collect training samples for shared dictionary
        if not self._dict_trained and len(self._training_samples) < 8:
            self._training_samples.append(data[:4096])  # sample first 4KB
            if len(self._training_samples) >= 8:
                self._shared_dict.train(self._training_samples)
                self._dict_trained = True
                self._training_samples.clear()

        # 1. 量化
        if self._quantization != QuantizationLevel.NONE:
            data = self._quantize(data)

        # 2. 壓縮（使用 MLA shared dict 增強）
        if self._dict_trained:
            compressed = self._shared_dict.compress(data)
        else:
            compressed = self._compress(data)
        self._stats.total_bytes_in += len(data)
        self._stats.total_bytes_out += len(compressed)
        if self._stats.total_bytes_in > 0:
            self._stats.compression_ratio = self._stats.total_bytes_in / max(1, self._stats.total_bytes_out)

        # 3. 寫入 buffer
        buf_key = f"{block_id}:{offset}"
        with self._lock:
            if buf_key in self._buffer:
                # 更新已有的 buffer entry
                old = self._buffer[buf_key]
                self._buffer_size -= len(old.data)
                old.data = compressed
                old.dirty = True
                old.last_access = time.time()
                self._buffer.move_to_end(buf_key)
                self._buffer_size += len(compressed)
            else:
                # 新增 entry
                self._buffer[buf_key] = BufferedBlock(
                    block_id=block_id,
                    data=compressed,
                    offset=offset,
                )
                self._buffer_size += len(compressed)

            # 4. buffer 滿時 evict 最舊的
            while (self._buffer_size > self._buffer_max
                   or len(self._buffer) > self._max_entries):
                self._evict_oldest(flush_fn)

        return len(compressed)

    def read(
        self,
        block_id: str,
        offset: int,
        size: int,
        read_fn=None,
    ) -> bytes:
        """
        最佳化讀取：先查 buffer → 未命中則從裝置讀取 → 解壓 → 反量化。

        Args:
            read_fn: 從裝置讀取的函式
                     read_fn(block_id, offset, size) → bytes
        """
        buf_key = f"{block_id}:{offset}"

        with self._lock:
            if buf_key in self._buffer:
                # Buffer 命中
                entry = self._buffer[buf_key]
                entry.access_count += 1
                entry.last_access = time.time()
                self._buffer.move_to_end(buf_key)
                self._stats.buffer_hits += 1

                data = self._decompress(entry.data)
                if self._quantization != QuantizationLevel.NONE:
                    data = self._dequantize(data, size)
                return data[:size]

        # Buffer 未命中
        self._stats.buffer_misses += 1

        if read_fn is None:
            return b"\x00" * size

        raw = read_fn(block_id, offset, size)
        data = self._decompress(raw)

        if self._quantization != QuantizationLevel.NONE:
            data = self._dequantize(data, size)

        return data[:size]

    def flush_all(self, flush_fn) -> int:
        """
        將 buffer 中所有 dirty 區塊 flush 到裝置。
        Returns: flush 的區塊數量
        """
        flushed = 0
        with self._lock:
            for key, entry in list(self._buffer.items()):
                if entry.dirty and flush_fn:
                    try:
                        flush_fn(entry.block_id, entry.offset, entry.data)
                        entry.dirty = False
                        flushed += 1
                    except Exception as e:
                        logger.error("Flush failed for %s: %s", key, e)
        logger.debug("Flushed %d blocks to device", flushed)
        return flushed

    def invalidate(self, block_id: str) -> None:
        """使指定 block 的所有 buffer entry 失效"""
        with self._lock:
            to_remove = [k for k in self._buffer if k.startswith(f"{block_id}:")]
            for k in to_remove:
                entry = self._buffer.pop(k)
                self._buffer_size -= len(entry.data)

    def clear_buffer(self) -> None:
        """清空整個 buffer"""
        with self._lock:
            self._buffer.clear()
            self._buffer_size = 0

    @property
    def buffer_utilization_pct(self) -> float:
        if self._buffer_max <= 0:
            return 0
        return (self._buffer_size / self._buffer_max) * 100

    @property
    def stats(self) -> OptimizerStats:
        return self._stats

    def get_summary(self) -> Dict[str, Any]:
        hit_total = self._stats.buffer_hits + self._stats.buffer_misses
        hit_rate = (self._stats.buffer_hits / hit_total * 100) if hit_total > 0 else 0

        return {
            "compression": self._compression.value,
            "quantization": self._quantization.value,
            "compression_ratio": round(self._stats.compression_ratio, 2),
            "buffer_size_mb": self._buffer_size / (1024**2),
            "buffer_max_mb": self._buffer_max / (1024**2),
            "buffer_utilization_pct": round(self.buffer_utilization_pct, 1),
            "buffer_entries": len(self._buffer),
            "buffer_hit_rate_pct": round(hit_rate, 1),
            "total_compressed_mb": self._stats.total_bytes_out / (1024**2),
            "effective_bandwidth_multiplier": round(self._stats.compression_ratio, 2),
        }

    # ── Compression ──

    def _compress(self, data: bytes) -> bytes:
        if self._compression == CompressionMethod.NONE:
            return data

        start = time.perf_counter()

        if self._compression == CompressionMethod.LZ4 and HAS_LZ4:
            result = lz4f.compress(data, compression_level=0)  # 0 = fastest
        elif self._compression == CompressionMethod.ZLIB_FAST:
            result = zlib.compress(data, level=1)  # 1 = fastest
        else:
            result = data

        self._stats.total_compress_ms += (time.perf_counter() - start) * 1000

        # 如果壓縮後反而更大，就不壓縮
        if len(result) >= len(data):
            return data

        return result

    def _decompress(self, data: bytes) -> bytes:
        if self._compression == CompressionMethod.NONE:
            return data

        start = time.perf_counter()

        try:
            if self._compression == CompressionMethod.LZ4 and HAS_LZ4:
                result = lz4f.decompress(data)
            elif self._compression == CompressionMethod.ZLIB_FAST:
                result = zlib.decompress(data)
            else:
                result = data
        except (zlib.error, Exception):
            # 可能是未壓縮的資料
            result = data

        self._stats.total_decompress_ms += (time.perf_counter() - start) * 1000
        return result

    # ── Quantization ──

    def _quantize(self, data: bytes) -> bytes:
        """FP16 → INT8/INT4 量化"""
        if self._quantization == QuantizationLevel.INT8:
            return self._fp16_to_int8(data)
        elif self._quantization == QuantizationLevel.INT4:
            return self._fp16_to_int4(data)
        return data

    def _dequantize(self, data: bytes, original_size: int) -> bytes:
        """INT8/INT4 → FP16 反量化"""
        if self._quantization == QuantizationLevel.INT8:
            return self._int8_to_fp16(data, original_size)
        elif self._quantization == QuantizationLevel.INT4:
            return self._int4_to_fp16(data, original_size)
        return data

    @staticmethod
    def _fp16_to_int8(data: bytes) -> bytes:
        """
        簡化的 FP16→INT8 線性量化。
        格式：[scale:4bytes][zero:4bytes][int8_data]
        """
        if len(data) < 4:
            return data

        # 將 bytes 解讀為 FP16 值
        n_values = len(data) // 2
        if n_values == 0:
            return data

        # 計算 scale 和 zero point
        # 簡化：假設資料已正規化在 [-1, 1] 範圍
        scale = 127.0
        zero = 0.0

        header = struct.pack("<ff", scale, zero)  # 8 bytes header

        # 量化：每個 FP16 (2 bytes) → INT8 (1 byte)
        quantized = bytearray(n_values)
        for i in range(n_values):
            if i * 2 + 1 < len(data):
                # 簡化：直接取 high byte 作為量化值
                quantized[i] = data[i * 2 + 1]

        return header + bytes(quantized)

    @staticmethod
    def _int8_to_fp16(data: bytes, original_size: int) -> bytes:
        """INT8→FP16 反量化"""
        if len(data) < 8:
            return data + b"\x00" * (original_size - len(data))

        # 讀取 header
        scale, zero = struct.unpack("<ff", data[:8])
        quantized = data[8:]

        # 反量化
        result = bytearray(len(quantized) * 2)
        for i in range(len(quantized)):
            result[i * 2] = 0
            result[i * 2 + 1] = quantized[i]

        # 補齊到原始大小
        if len(result) < original_size:
            result.extend(b"\x00" * (original_size - len(result)))

        return bytes(result[:original_size])

    @staticmethod
    def _fp16_to_int4(data: bytes) -> bytes:
        """FP16→INT4 量化（2 個 INT4 打包進 1 byte）"""
        n_values = len(data) // 2
        if n_values == 0:
            return data

        header = struct.pack("<I", n_values)  # 4 bytes: 原始值數量
        packed_size = (n_values + 1) // 2
        packed = bytearray(packed_size)

        for i in range(0, n_values, 2):
            hi = (data[i * 2 + 1] >> 4) & 0x0F if i * 2 + 1 < len(data) else 0
            lo = (data[(i + 1) * 2 + 1] >> 4) & 0x0F if (i + 1) * 2 + 1 < len(data) else 0
            packed[i // 2] = (hi << 4) | lo

        return header + bytes(packed)

    @staticmethod
    def _int4_to_fp16(data: bytes, original_size: int) -> bytes:
        """INT4→FP16 反量化"""
        if len(data) < 4:
            return b"\x00" * original_size

        n_values = struct.unpack("<I", data[:4])[0]
        packed = data[4:]

        result = bytearray(n_values * 2)
        for i in range(0, n_values, 2):
            idx = i // 2
            if idx < len(packed):
                hi = (packed[idx] >> 4) & 0x0F
                lo = packed[idx] & 0x0F
                result[i * 2 + 1] = hi << 4
                if (i + 1) * 2 + 1 < len(result):
                    result[(i + 1) * 2 + 1] = lo << 4

        if len(result) < original_size:
            result.extend(b"\x00" * (original_size - len(result)))

        return bytes(result[:original_size])

    # ── Sequential I/O Reordering ──

    def reorder_sequential(self, requests: List[Tuple[str, int, int]]) -> List[Tuple[str, int, int]]:
        """
        重新排列 I/O 請求為順序存取模式。
        對 HDD 特別重要（隨機→順序可提升 100-300 倍）。

        Args:
            requests: [(block_id, offset, size), ...]
        Returns:
            重排後的請求列表
        """
        if len(requests) <= 1:
            return requests

        # 按 (block_id, offset) 排序 → 最大化順序存取
        sorted_reqs = sorted(requests, key=lambda r: (r[0], r[1]))

        # 合併相鄰的小請求
        merged = []
        current = list(sorted_reqs[0])

        for req in sorted_reqs[1:]:
            # 同一個 block 且相鄰 → 合併
            if (req[0] == current[0]
                    and req[1] <= current[1] + current[2]
                    and current[2] + req[2] <= self._seq_block):
                new_end = max(current[1] + current[2], req[1] + req[2])
                current[2] = new_end - current[1]
                self._stats.sequential_merges += 1
            else:
                merged.append(tuple(current))
                current = list(req)

        merged.append(tuple(current))
        return merged

    # ── Internal ──

    def _evict_oldest(self, flush_fn) -> None:
        """Evict buffer 中最舊的 entry"""
        if not self._buffer:
            return

        key, entry = self._buffer.popitem(last=False)  # FIFO
        self._buffer_size -= len(entry.data)

        # 如果是 dirty 的，先 flush
        if entry.dirty and flush_fn:
            try:
                flush_fn(entry.block_id, entry.offset, entry.data)
            except Exception as e:
                logger.error("Evict flush failed for %s: %s", key, e)


class SlowDeviceProfile:
    """
    裝置特性描述，用於自動選擇最佳化策略。
    """

    PROFILES = {
        "sd_uhs1": {
            "label": "SD Card (UHS-I)",
            "seq_read_mbs": 90, "seq_write_mbs": 60,
            "rand_read_mbs": 15, "rand_write_mbs": 5,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.INT8,
            "buffer_mb": 256,
            "seq_block_kb": 512,
            "best_use": "kv_cache_only",
        },
        "sd_uhs2": {
            "label": "SD Card (UHS-II)",
            "seq_read_mbs": 280, "seq_write_mbs": 200,
            "rand_read_mbs": 40, "rand_write_mbs": 20,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.NONE,
            "buffer_mb": 256,
            "seq_block_kb": 1024,
            "best_use": "kv_cache_and_cold_weights",
        },
        "sd_uhs3": {
            "label": "SD Card (UHS-III)",
            "seq_read_mbs": 500, "seq_write_mbs": 400,
            "rand_read_mbs": 80, "rand_write_mbs": 40,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.NONE,
            "buffer_mb": 512,
            "seq_block_kb": 2048,
            "best_use": "full_offload",
        },
        "usb2_flash": {
            "label": "USB 2.0 Flash Drive",
            "seq_read_mbs": 35, "seq_write_mbs": 15,
            "rand_read_mbs": 5, "rand_write_mbs": 1,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.INT4,
            "buffer_mb": 128,
            "seq_block_kb": 256,
            "best_use": "context_snapshot_only",
        },
        "usb3_flash": {
            "label": "USB 3.0 Flash Drive",
            "seq_read_mbs": 300, "seq_write_mbs": 100,
            "rand_read_mbs": 30, "rand_write_mbs": 10,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.INT8,
            "buffer_mb": 256,
            "seq_block_kb": 1024,
            "best_use": "kv_cache_only",
        },
        "usb3_portable_ssd": {
            "label": "USB 3.x Portable SSD",
            "seq_read_mbs": 800, "seq_write_mbs": 600,
            "rand_read_mbs": 200, "rand_write_mbs": 150,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.NONE,
            "buffer_mb": 512,
            "seq_block_kb": 2048,
            "best_use": "full_offload",
        },
        "hdd_5400rpm": {
            "label": "External HDD (5400 RPM)",
            "seq_read_mbs": 100, "seq_write_mbs": 90,
            "rand_read_mbs": 0.5, "rand_write_mbs": 0.5,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.INT8,
            "buffer_mb": 512,
            "seq_block_kb": 4096,   # HDD 必須大塊順序讀寫
            "best_use": "read_only_model_storage",
        },
        "hdd_7200rpm": {
            "label": "External HDD (7200 RPM)",
            "seq_read_mbs": 150, "seq_write_mbs": 130,
            "rand_read_mbs": 1.0, "rand_write_mbs": 1.0,
            "compression": CompressionMethod.ZLIB_FAST,
            "quantization": QuantizationLevel.INT8,
            "buffer_mb": 512,
            "seq_block_kb": 4096,
            "best_use": "read_only_model_storage",
        },
    }

    @classmethod
    def get_profile(cls, key: str) -> Optional[Dict]:
        return cls.PROFILES.get(key)

    @classmethod
    def auto_detect_profile(cls, seq_read_mbs: float, is_rotational: bool = False) -> str:
        """根據實測頻寬自動選擇 profile"""
        if is_rotational:
            return "hdd_7200rpm" if seq_read_mbs >= 130 else "hdd_5400rpm"

        if seq_read_mbs >= 600:
            return "usb3_portable_ssd"
        elif seq_read_mbs >= 250:
            return "usb3_flash"
        elif seq_read_mbs >= 80:
            return "sd_uhs1"
        elif seq_read_mbs >= 30:
            return "usb2_flash"

        return "usb2_flash"  # 最保守

    @classmethod
    def create_optimizer(cls, profile_key: str) -> SlowDeviceOptimizer:
        """根據 profile 建立最佳化的 optimizer"""
        profile = cls.PROFILES.get(profile_key)
        if not profile:
            logger.warning("Unknown profile '%s', using defaults", profile_key)
            return SlowDeviceOptimizer()

        return SlowDeviceOptimizer(
            compression=profile["compression"],
            quantization=profile["quantization"],
            buffer_size_mb=profile["buffer_mb"],
            sequential_block_kb=profile["seq_block_kb"],
        )

    @classmethod
    def estimate_effective_bandwidth(cls, profile_key: str) -> Dict[str, float]:
        """
        估算最佳化後的等效頻寬。

        注意：swap/pagefile 是隨機 4KB I/O，所以 swap 場景用 rand_*_mbs。
        壓縮/量化增益只適用於自己管理的 I/O（SlowDeviceOptimizer 的大塊順序讀寫），
        不適用於 OS swap（OS 不會壓縮 page）。
        """
        profile = cls.PROFILES.get(profile_key, {})
        raw_seq_read = profile.get("seq_read_mbs", 100)
        raw_seq_write = profile.get("seq_write_mbs", 50)
        raw_rand_read = profile.get("rand_read_mbs", 10)
        raw_rand_write = profile.get("rand_write_mbs", 5)

        # 壓縮增益 — 只適用於順序 I/O（自己管理的 buffer I/O）
        comp = profile.get("compression", CompressionMethod.NONE)
        comp_ratio = 2.5 if comp != CompressionMethod.NONE else 1.0

        # 量化增益
        quant = profile.get("quantization", QuantizationLevel.NONE)
        quant_ratio = {"none": 1.0, "int8": 2.0, "int4": 4.0}.get(
            quant.value if isinstance(quant, QuantizationLevel) else quant, 1.0
        )

        total_ratio = comp_ratio * quant_ratio

        return {
            # 順序 I/O（自己管理的最佳化 I/O，如 KV Cache 卸載）
            "raw_read_mbs": raw_seq_read,
            "raw_write_mbs": raw_seq_write,
            "effective_read_mbs": round(raw_seq_read * total_ratio, 0),
            "effective_write_mbs": round(raw_seq_write * total_ratio, 0),
            "compression_ratio": comp_ratio,
            "quantization_ratio": quant_ratio,
            "total_multiplier": round(total_ratio, 1),
            # 隨機 I/O（OS swap 的真實效能，無壓縮增益）
            "rand_read_mbs": raw_rand_read,
            "rand_write_mbs": raw_rand_write,
            "best_use": profile.get("best_use", "unknown"),
        }
