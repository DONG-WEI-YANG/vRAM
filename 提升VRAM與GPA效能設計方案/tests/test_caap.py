"""
Tests for Compression-Aware Adaptive Prefetch (CAAP) Algorithm
================================================================
Validates the per-layer prefix-sum lookahead, compression ratio feedback,
and adaptive behavior under varying compression profiles.
"""

import unittest
from unittest.mock import MagicMock, PropertyMock
from collections import deque

from microsystems.core.prefetcher import (
    PredictivePrefetcher, PrefetchStrategy, LayerProfile,
    PrefetchPlan, PrefetchStats,
)
from microsystems.core.memory_pool import MemoryPool, MemoryTier


def _make_pool_and_transfer(layers):
    """Build mock pool + transfer for testing."""
    pool = MagicMock(spec=MemoryPool)

    def get_block(bid):
        blk = MagicMock()
        blk.tier = MemoryTier.EXTERNAL
        blk.size_bytes = 100 * 1024 * 1024  # 100MB
        return blk

    pool.get_block = get_block

    transfer = MagicMock()
    transfer.stats = MagicMock()
    transfer.stats.avg_throughput_mbs = 500.0  # 500 MB/s baseline
    return pool, transfer


# ── LayerProfile compression tracking ──────────────────────────────


class TestLayerProfileCompression(unittest.TestCase):
    """Test per-layer compression ratio EWMA tracking."""

    def test_initial_ratio_is_1(self):
        lp = LayerProfile("layer_0", "blk_0", size_bytes=100_000_000)
        self.assertEqual(lp.compression_ratio, 1.0)
        self.assertEqual(lp.physical_size_bytes, 100_000_000)

    def test_first_observation_sets_ratio(self):
        lp = LayerProfile("layer_0", "blk_0", size_bytes=100_000_000)
        lp.update_compression_ratio(logical=100_000_000, physical=25_000_000)
        # First observation: ratio = 100M / 25M = 4.0 (no EWMA blending)
        self.assertAlmostEqual(lp.compression_ratio, 4.0, places=1)

    def test_ewma_blending(self):
        lp = LayerProfile("layer_0", "blk_0", size_bytes=100_000_000)
        # First: ratio = 4.0
        lp.update_compression_ratio(100_000_000, 25_000_000)
        # Second: observed = 2.0, EWMA = 0.3*2.0 + 0.7*4.0 = 3.4
        lp.update_compression_ratio(100_000_000, 50_000_000)
        self.assertAlmostEqual(lp.compression_ratio, 3.4, places=1)

    def test_physical_size_reflects_compression(self):
        lp = LayerProfile("layer_0", "blk_0", size_bytes=100_000_000)
        lp.update_compression_ratio(100_000_000, 25_000_000)  # ratio = 4.0
        # physical_size = 100M / 4.0 = 25M
        self.assertAlmostEqual(lp.physical_size_bytes, 25_000_000, delta=1)

    def test_zero_physical_is_ignored(self):
        lp = LayerProfile("layer_0", "blk_0", size_bytes=100_000_000)
        lp.update_compression_ratio(100_000_000, 0)  # invalid
        self.assertEqual(lp.compression_ratio, 1.0)  # unchanged


# ── CAAP Lookahead Algorithm ───────────────────────────────────────


class TestCAAPLookahead(unittest.TestCase):
    """Test the per-layer prefix-sum lookahead computation."""

    def _make_prefetcher(self, layers, bw_mbs=500.0):
        pool, transfer = _make_pool_and_transfer(layers)
        transfer.stats.avg_throughput_mbs = bw_mbs
        pf = PredictivePrefetcher(pool, transfer, lookahead=2,
                                   strategy=PrefetchStrategy.SEQUENTIAL)
        pf.register_model_layers(layers)
        pf._current_layer_idx = 0
        return pf

    def test_uniform_layers_fast_device(self):
        """All layers same size, fast device → deep lookahead."""
        layers = []
        for i in range(10):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=100 * 1024 * 1024)
            lp.compute_time_ms = 50.0  # 50ms GPU compute
            # 100MB / 500MB/s = 200ms load, but with 3x compression → 66ms
            lp.update_compression_ratio(100 * 1024 * 1024, 33 * 1024 * 1024)
            layers.append(lp)

        pf = self._make_prefetcher(layers, bw_mbs=500.0)
        # 填入 compute history 讓演算法啟動
        for _ in range(5):
            pf._compute_history.append(50.0)

        lookahead = pf._compute_effective_lookahead(500.0, from_layer=0)
        # physical ~33MB, load ~66ms, compute ~50ms
        # cumulative: load 66 vs compute 50 → offset 1: 66 > 50 → break at 1
        # But we still get at least base lookahead (2) + speculative
        self.assertGreaterEqual(lookahead, 2)

    def test_high_compression_deepens_lookahead(self):
        """Highly compressible layers → can prefetch much deeper."""
        layers = []
        for i in range(10):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=100 * 1024 * 1024)
            lp.compute_time_ms = 100.0  # 100ms compute
            # 6x compression: 100MB → 16.7MB physical
            lp.update_compression_ratio(100 * 1024 * 1024, 16_700_000)
            layers.append(lp)

        pf = self._make_prefetcher(layers, bw_mbs=500.0)
        for _ in range(5):
            pf._compute_history.append(100.0)

        lookahead = pf._compute_effective_lookahead(500.0, from_layer=0)
        # physical ~16.7MB, load ~33ms, compute ~100ms
        # ratio load/compute = 0.33 → can prefetch ~3 layers per compute slot
        # prefix sum: L=3 → io=99 vs compute=300 ✓, L=8 cap
        self.assertGreaterEqual(lookahead, 3)

    def test_no_compression_shallower_than_high_compression(self):
        """Uncompressible layers → shallower lookahead than high-compression layers.

        Use heavy compute (200ms) so CAAP prefix-sum dominates over speculative floor.
        """
        layers = []
        for i in range(10):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=200 * 1024 * 1024)
            lp.compute_time_ms = 200.0
            lp.update_compression_ratio(200 * 1024 * 1024, 180 * 1024 * 1024)  # 1.1x
            layers.append(lp)

        pf = self._make_prefetcher(layers, bw_mbs=500.0)
        for _ in range(5):
            pf._compute_history.append(200.0)

        lookahead_low = pf._compute_effective_lookahead(500.0, from_layer=0)

        # Now switch to high compression (6x)
        for lp in layers:
            lp.update_compression_ratio(200 * 1024 * 1024, 33 * 1024 * 1024)
        lookahead_high = pf._compute_effective_lookahead(500.0, from_layer=0)

        # Core CAAP property: high compression → deeper lookahead
        # 1.1x: physical 180MB, load 360ms vs compute 200ms → io-bound, CAAP ~4-5
        # 6x:   physical 33MB, load 66ms vs compute 200ms → compute-bound, CAAP ~8
        self.assertGreater(lookahead_high, lookahead_low)

    def test_mixed_compression_profile(self):
        """
        Mixed layers: attention (high compress) + FFN (medium) + embedding (low).
        Validates per-layer handling, not just average.
        """
        layers = []
        profiles = [
            ("attn_0", 6.0, 80.0),   # Attention: 6x compress, 80ms compute
            ("ffn_0",  2.5, 120.0),   # FFN: 2.5x compress, 120ms compute
            ("emb_0",  1.2, 30.0),    # Embedding: 1.2x compress, 30ms compute
            ("attn_1", 6.0, 80.0),
            ("ffn_1",  2.5, 120.0),
            ("emb_1",  1.2, 30.0),
            ("attn_2", 6.0, 80.0),
            ("ffn_2",  2.5, 120.0),
        ]
        for name, ratio, comp_ms in profiles:
            lp = LayerProfile(name, f"blk_{name}", size_bytes=100 * 1024 * 1024)
            lp.compute_time_ms = comp_ms
            physical = int(100 * 1024 * 1024 / ratio)
            lp.update_compression_ratio(100 * 1024 * 1024, physical)
            layers.append(lp)

        pf = self._make_prefetcher(layers, bw_mbs=500.0)
        for _ in range(5):
            pf._compute_history.append(80.0)

        lookahead = pf._compute_effective_lookahead(500.0, from_layer=0)
        # This should be deeper than uniform-no-compression case
        # because attention layers (6x) are very cheap to transfer
        self.assertGreaterEqual(lookahead, 3)

    def test_slow_device_shallower_than_fast(self):
        """Slow device (100 MB/s) → shallower lookahead than fast device (2000 MB/s)."""
        layers = []
        for i in range(10):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=100 * 1024 * 1024)
            lp.compute_time_ms = 50.0
            lp.update_compression_ratio(100 * 1024 * 1024, 33 * 1024 * 1024)  # 3x
            layers.append(lp)

        pf_slow = self._make_prefetcher(layers, bw_mbs=100.0)
        for _ in range(5):
            pf_slow._compute_history.append(50.0)
        lookahead_slow = pf_slow._compute_effective_lookahead(100.0, from_layer=0)

        pf_fast = self._make_prefetcher(layers, bw_mbs=2000.0)
        for _ in range(5):
            pf_fast._compute_history.append(50.0)
        lookahead_fast = pf_fast._compute_effective_lookahead(2000.0, from_layer=0)

        # Core: faster device → deeper lookahead
        self.assertGreater(lookahead_fast, lookahead_slow)

    def test_fast_device_with_compression_max_lookahead(self):
        """SD Express Gen4 (2000 MB/s) + 6x compression → hit max lookahead."""
        layers = []
        for i in range(10):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=100 * 1024 * 1024)
            lp.compute_time_ms = 200.0  # heavy compute
            lp.update_compression_ratio(100 * 1024 * 1024, 16_700_000)  # 6x
            layers.append(lp)

        pf = self._make_prefetcher(layers, bw_mbs=2000.0)
        for _ in range(5):
            pf._compute_history.append(200.0)

        lookahead = pf._compute_effective_lookahead(2000.0, from_layer=0)
        # physical 16.7MB / 2000 MB/s = 8.3ms, compute 200ms
        # ratio = 0.04 → can prefetch 24 layers per compute slot → capped at 8
        self.assertEqual(lookahead, 8)


# ���─ Feedback Loop ──────────────────────────────────────────────────


class TestCAAPFeedbackLoop(unittest.TestCase):
    """Test that report_transfer_stats feeds back into lookahead."""

    def test_feedback_updates_layer_ratio(self):
        layers = [
            LayerProfile("L0", "blk_0", size_bytes=100_000_000),
            LayerProfile("L1", "blk_1", size_bytes=100_000_000),
        ]
        pool, transfer = _make_pool_and_transfer(layers)
        pf = PredictivePrefetcher(pool, transfer, lookahead=2)
        pf.register_model_layers(layers)

        # Initially ratio = 1.0
        self.assertEqual(pf._layers[0].compression_ratio, 1.0)

        # Report actual transfer: 100MB logical → 20MB physical (5x)
        pf.report_transfer_stats(0, logical_bytes=100_000_000, physical_bytes=20_000_000)
        self.assertAlmostEqual(pf._layers[0].compression_ratio, 5.0, places=1)

        # Report again: observed 3x → EWMA blends
        pf.report_transfer_stats(0, logical_bytes=100_000_000, physical_bytes=33_000_000)
        # 0.3 * 3.03 + 0.7 * 5.0 ≈ 4.41
        self.assertGreater(pf._layers[0].compression_ratio, 4.0)
        self.assertLess(pf._layers[0].compression_ratio, 5.0)

    def test_stats_track_avg_compression(self):
        layers = []
        for i in range(4):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=100_000_000)
            lp.compute_time_ms = 100.0
            layers.append(lp)

        pool, transfer = _make_pool_and_transfer(layers)
        pf = PredictivePrefetcher(pool, transfer, lookahead=2)
        pf.register_model_layers(layers)
        pf._current_layer_idx = 0
        for _ in range(5):
            pf._compute_history.append(100.0)

        # Report mixed compression ratios
        pf.report_transfer_stats(0, 100_000_000, 25_000_000)  # 4x
        pf.report_transfer_stats(1, 100_000_000, 50_000_000)  # 2x
        pf.report_transfer_stats(2, 100_000_000, 16_000_000)  # 6.25x

        # Trigger lookahead computation (which updates stats)
        pf._compute_effective_lookahead(500.0)
        # avg of layers with samples: (4.0 + 2.0 + 6.25) / 3 ≈ 4.08
        self.assertGreater(pf._stats.avg_compression_ratio, 3.0)


# ── Integration: CAAP with Strategy Dispatch ──────────────────────


class TestCAAPWithStrategies(unittest.TestCase):
    """Test that CAAP integrates correctly with all 4 strategies."""

    def _make_layers(self, n=8, compression=3.0, compute_ms=100.0):
        layers = []
        for i in range(n):
            lp = LayerProfile(f"L{i}", f"blk_{i}", size_bytes=100 * 1024 * 1024)
            lp.compute_time_ms = compute_ms
            physical = int(100 * 1024 * 1024 / compression)
            lp.update_compression_ratio(100 * 1024 * 1024, physical)
            layers.append(lp)
        return layers

    def _make_pf(self, strategy, layers, bw=500.0):
        pool, transfer = _make_pool_and_transfer(layers)
        transfer.stats.avg_throughput_mbs = bw
        pf = PredictivePrefetcher(pool, transfer, lookahead=2, strategy=strategy)
        pf.register_model_layers(layers)
        pf._current_layer_idx = 0
        for _ in range(5):
            pf._compute_history.append(100.0)
        return pf

    def test_sequential_uses_physical_size(self):
        layers = self._make_layers(compression=4.0)
        pf = self._make_pf(PrefetchStrategy.SEQUENTIAL, layers)
        plan = pf.create_prefetch_plan(0)
        # Should have targets using physical-size-based time estimation
        self.assertGreater(len(plan.prefetch_targets), 0)
        self.assertGreater(plan.estimated_save_ms, 0)

    def test_adaptive_uses_compression(self):
        layers = self._make_layers(compression=6.0, compute_ms=200.0)
        pf = self._make_pf(PrefetchStrategy.ADAPTIVE, layers)
        plan = pf.create_prefetch_plan(0)
        # High compression + slow compute → should prefetch deeply
        self.assertGreaterEqual(len(plan.prefetch_targets), 3)

    def test_kv_cache_forward_backward(self):
        layers = self._make_layers(compression=3.0)
        pf = self._make_pf(PrefetchStrategy.KV_CACHE, layers)
        # Set current to middle
        pf._current_layer_idx = 4
        plan = pf.create_prefetch_plan(4)
        self.assertGreater(len(plan.prefetch_targets), 0)


if __name__ == "__main__":
    unittest.main()
