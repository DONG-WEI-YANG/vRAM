# Context Window 與 KV Cache 研究筆記

## KV Cache 記憶體計算公式 (來源: Lyceum Technology, 2026/2/23)

### MHA (Multi-Head Attention) 公式：
KV_cache_bytes = 2 × layers × heads × head_dim × seq_len × batch_size × bytes_per_element

### GQA (Grouped-Query Attention) 公式：
KV_cache_bytes = 2 × layers × kv_heads × head_dim × seq_len × batch_size × bytes_per_element

### 範例計算 (Llama-3-8B, MHA):
- layers=32, heads=32, head_dim=128, seq_len=2048, batch=1, FP16(2 bytes)
- = 2 × 32 × 32 × 128 × 2048 × 1 × 2 = 1,073,741,824 bytes = 1 GB

### 關鍵事實：
- KV Cache 隨 context length 線性增長 (O(n))
- Attention 計算隨 context length 二次增長 (O(n^2))
- 4K → 32K tokens: KV Cache 增加 8 倍
- GQA (如 Llama-3) 可將 KV Cache 減少 4 倍 (32 heads → 8 kv_heads)
- FP8 量化可再減半

## 各模型 KV Cache 大小 (每 token, FP16)
- Llama-3-8B: 32 layers × 8 kv_heads × 128 dim × 2(KV) × 2 bytes = 131,072 bytes/token ≈ 0.125 MB/token
- Llama-3-70B: 80 layers × 8 kv_heads × 128 dim × 2(KV) × 2 bytes = 327,680 bytes/token ≈ 0.3125 MB/token
- Llama-3-8B @ 128K context: 0.125 MB × 128,000 = 16 GB (僅 KV Cache!)
- Llama-3-70B @ 128K context: 0.3125 MB × 128,000 = 40 GB (僅 KV Cache!)

## 核心洞察：SD 卡如何提升 Context Window
1. 模型權重是固定的，載入後不變
2. KV Cache 隨對話長度持續增長，是 VRAM 爆炸的主因
3. 將 KV Cache 卸載到 SD 卡 = 直接提升可用 Context Window
4. KV Cache 的存取模式：寫入一次、讀取多次 → 適合 SD 卡的讀取優勢
5. 配合 KV Cache 壓縮技術 (kvpress) 可進一步提升

## KV Cache 卸載效能數據 (來源: BentoML LLM Inference Handbook, NVIDIA)

### 關鍵效能數據：
- NVIDIA 報告：KV Cache 卸載可實現最高 14 倍 TTFT 加速（相比從頭重算 KV Cache）
- LMCache + vLLM 組合：3-10 倍延遲降低
- 核心原理：不是所有 KV Cache 都需要隨時在 GPU 記憶體中
- 閒置的 KV Cache 可以卸載到 CPU RAM、SSD、甚至遠端儲存

### SD 卡作為 KV Cache 卸載目標的優勢：
1. 熱插拔：不同對話/專案可以存在不同 SD 卡上
2. 持久化：關機後 KV Cache 不會消失，下次啟動可直接載入
3. 容量大：1TB SD Express 卡可儲存大量 KV Cache
4. 成本低：比增加 VRAM 或系統 RAM 便宜得多

### Context Window 提升計算：
假設 RTX 4070 (12GB VRAM)，運行 Llama-3-8B (約 4.5GB 權重)：
- 原始可用 KV Cache 空間：12GB - 4.5GB - 1GB(overhead) = 6.5GB
- 每 token KV Cache：0.125 MB (FP16) 或 0.0625 MB (INT8)
- 原始 Context Window：6.5GB / 0.125MB = 52,000 tokens (FP16)
- 加入 512GB SD Express 卡後：
  - 可用 KV Cache 空間：6.5GB + 512GB = 518.5GB
  - 理論 Context Window：518.5GB / 0.125MB = 4,148,000 tokens (FP16)
  - 提升倍數：約 80 倍！
- 但實際受限於 SD 卡頻寬，超長 context 的 prefill 會變慢
