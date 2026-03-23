# SD-VRAM Booster — 開發紀錄 (Agent Log)

**製作者**: Peter Yang
**建立日期**: 2026-03-21

---

## 設計決策紀錄

### 1. 為什麼選擇 SD Express 卡？
- SD Express 使用與 NVMe SSD 完全相同的 PCIe + NVMe 協定
- 既然 SSD 可以擴展 VRAM（如 GreenBoost、Phison aiDAPTIV+），SD Express 卡也可以
- SD 卡的獨特優勢：熱插拔、攜帶性、可作為「AI 模型卡匣」

### 2. 為什麼使用純 Python 標準庫？
- 使用者不需要安裝任何額外套件
- tkinter 是 Python 內建的 GUI 框架
- 透過 PyInstaller 打包後，使用者甚至不需要安裝 Python

### 3. 為什麼採用三層記憶體架構？
- Tier 0 (VRAM): 放模型權重和活躍 KV Cache
- Tier 1 (RAM): 作為緩衝層
- Tier 2 (SD Card): 大容量 KV Cache 卸載
- 這與 NVIDIA 的 Storage Next 和 Phison aiDAPTIV+ 的設計理念一致

### 4. 隨插即用設計
- 使用者要求「插入後只需確認是否使用」
- Windows: autorun.inf + bat 腳本
- Linux: udev 規則自動觸發
- 系統自動偵測 OS、GPU、SD 卡規格

### 5. 效能數據的誠實性
- 所有效能預估基於物理頻寬極限計算
- 明確標示 SD 卡頻寬遠低於 VRAM（1:70 ~ 1:260）
- 核心價值不是「加速」，而是「讓無法運行的模型可以運行」

## 技術參考

- NVIDIA GreenBoost: 開源 NVMe VRAM 擴展
- Phison aiDAPTIV+: SSD 擴展 GPU 記憶體商業方案
- SD Express 規格: SD 8.0/9.0，PCIe Gen3/Gen4 + NVMe
- NVIDIA Storage Next: 企業級 GPU 記憶體擴展
