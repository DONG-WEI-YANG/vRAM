"""
System-wide configuration for VRAM Booster micro-systems.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional


class DeviceType(str, Enum):
    SD_EXPRESS = "sd_express"
    USB_STORAGE = "usb_storage"
    ENCLOSURE_NVME = "enclosure_nvme"


class BoostMode(str, Enum):
    AUTO = "auto"              # 自動偵測最佳裝置
    KV_CACHE_ONLY = "kv_only"  # 僅卸載 KV Cache
    FULL_OFFLOAD = "full"      # 完整模型權重 + KV Cache
    CONTEXT_EXTEND = "context"  # 專注 Context Window 擴展


@dataclass
class TierConfig:
    """單一記憶體層級的配置"""
    max_size_gb: float = 0.0
    priority: int = 0            # 0=highest (VRAM), 1=RAM, 2=external
    min_bandwidth_mbs: float = 0.0
    max_latency_us: float = 0.0


@dataclass
class SystemConfig:
    """微系統全域配置"""

    # ── 通用設定 ──
    device_type: DeviceType = DeviceType.SD_EXPRESS
    boost_mode: BoostMode = BoostMode.AUTO
    platform: str = field(default_factory=lambda: platform.system().lower())

    # ── 記憶體分層配置 ──
    tier_vram: TierConfig = field(default_factory=lambda: TierConfig(
        max_size_gb=0,  # 0 = auto-detect
        priority=0,
        min_bandwidth_mbs=300_000,
        max_latency_us=0.01,
    ))
    tier_ram: TierConfig = field(default_factory=lambda: TierConfig(
        max_size_gb=0,
        priority=1,
        min_bandwidth_mbs=30_000,
        max_latency_us=0.1,
    ))
    tier_external: TierConfig = field(default_factory=lambda: TierConfig(
        max_size_gb=0,
        priority=2,
        min_bandwidth_mbs=200,
        max_latency_us=100,
    ))

    # ── CUDA 攔截設定 ──
    cuda_intercept_enabled: bool = True
    cuda_alloc_threshold_mb: int = 256   # 大於此值的分配才重定向
    cuda_report_extended_mem: bool = True  # 向應用回報擴展後總容量

    # ── 預取設定 ──
    prefetch_enabled: bool = True
    prefetch_lookahead_layers: int = 2   # 預取未來 N 層
    prefetch_buffer_mb: int = 512        # 預取緩衝區大小

    # ── 健康監控 ──
    health_check_interval_s: float = 5.0
    temp_warning_celsius: float = 70.0
    temp_critical_celsius: float = 85.0
    min_tbw_warning_pct: float = 10.0    # 剩餘壽命低於 10% 告警

    # ── 熱插拔防護 ──
    hotplug_enabled: bool = True
    fallback_to_ram: bool = True
    graceful_shutdown_timeout_s: float = 3.0

    # ── 效能 ──
    io_queue_depth: int = 32
    block_size_kb: int = 4096            # 4MB blocks for sequential
    random_block_size_kb: int = 4        # 4KB for random access

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "SystemConfig":
        data = json.loads(path.read_text())
        data["device_type"] = DeviceType(data["device_type"])
        data["boost_mode"] = BoostMode(data["boost_mode"])
        for key in ("tier_vram", "tier_ram", "tier_external"):
            data[key] = TierConfig(**data[key])
        return cls(**data)


# ── 各裝置的預設頻寬/延遲參數 ──

DEVICE_SPECS = {
    # SD Express
    "sd_gen3_x1": {"bandwidth_mbs": 985, "latency_us": 12, "label": "SD Express Gen3 x1"},
    "sd_gen3_x2": {"bandwidth_mbs": 1970, "latency_us": 10, "label": "SD Express Gen3 x2"},
    "sd_gen4_x1": {"bandwidth_mbs": 1970, "latency_us": 10, "label": "SD Express Gen4 x1"},
    "sd_gen4_x2": {"bandwidth_mbs": 3940, "latency_us": 8, "label": "SD Express Gen4 x2"},
    # USB
    "usb3_gen1": {"bandwidth_mbs": 500, "latency_us": 200, "label": "USB 3.2 Gen1"},
    "usb3_gen2": {"bandwidth_mbs": 1000, "latency_us": 150, "label": "USB 3.2 Gen2"},
    "usb3_gen2x2": {"bandwidth_mbs": 1800, "latency_us": 120, "label": "USB 3.2 Gen2x2"},
    "usb4_v1": {"bandwidth_mbs": 3800, "latency_us": 15, "label": "USB4 v1 (40Gbps)"},
    "usb4_v2": {"bandwidth_mbs": 7500, "latency_us": 12, "label": "USB4 v2 (80Gbps)"},
    # Enclosure (PCIe Tunneling)
    "tb3": {"bandwidth_mbs": 2800, "latency_us": 18, "label": "Thunderbolt 3"},
    "tb4": {"bandwidth_mbs": 3000, "latency_us": 15, "label": "Thunderbolt 4"},
    "tb5": {"bandwidth_mbs": 10000, "latency_us": 10, "label": "Thunderbolt 5"},
    "usb4_enc_v1": {"bandwidth_mbs": 3800, "latency_us": 15, "label": "USB4 Enclosure v1"},
    "usb4_enc_v2": {"bandwidth_mbs": 7500, "latency_us": 12, "label": "USB4 Enclosure v2"},
}

# ── LLM 模型參數（用於效能預估）──

MODEL_PROFILES = {
    "llama3_8b_q4": {
        "name": "Llama-3 8B (Q4)",
        "weight_gb": 4.5,
        "kv_bytes_per_token": 65536,   # 0.0625 MB (INT8)
        "layers": 32,
    },
    "llama3_8b_q8": {
        "name": "Llama-3 8B (Q8)",
        "weight_gb": 8.5,
        "kv_bytes_per_token": 131072,  # 0.125 MB (FP16)
        "layers": 32,
    },
    "llama3_70b_q4": {
        "name": "Llama-3 70B (Q4)",
        "weight_gb": 40.0,
        "kv_bytes_per_token": 163840,  # 0.15625 MB (INT8)
        "layers": 80,
    },
    "llama3_70b_fp16": {
        "name": "Llama-3 70B (FP16)",
        "weight_gb": 140.0,
        "kv_bytes_per_token": 327680,  # 0.3125 MB (FP16)
        "layers": 80,
    },
}
