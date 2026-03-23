"""
VRAM Booster MCP Server
========================
將 VRAM Booster 包裝為 Model Context Protocol (MCP) Server，
讓 AI CLI（Claude Code / OSS120B / Cursor）可以直接呼叫
VRAM 管理功能。

協定：JSON-RPC 2.0 over stdio
啟動：python -m microsystems.mcp_server

MCP 工具清單：
  vram_scan       — 掃描所有可用裝置
  vram_activate   — 啟動 VRAM 擴展
  vram_deactivate — 停止 VRAM 擴展
  vram_status     — 取得即時狀態
  vram_estimate   — 效能預估
  vram_allocate   — 分配記憶體區塊
  vram_free       — 釋放記憶體區塊
  vram_health     — 裝置健康狀態
  vram_audit      — 查詢 audit log

配置（.mcp.json 範例）：
{
  "mcpServers": {
    "vram-booster": {
      "command": "python",
      "args": ["-m", "microsystems.mcp_server"],
      "cwd": "/path/to/project"
    }
  }
}
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── MCP Tool Schema Definitions ──

MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "vram_scan",
        "description": (
            "Scan system for all available VRAM expansion devices "
            "(SD Express, USB Storage, NVMe Enclosures). "
            "Returns GPU info, RAM size, and detected devices with bandwidth/latency specs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "vram_activate",
        "description": (
            "Activate VRAM expansion using the best available device. "
            "Initializes 3-tier memory pool (VRAM→RAM→External), "
            "starts CUDA interceptor, prefetcher, and health monitor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_type": {
                    "type": "string",
                    "enum": ["auto", "sd", "usb", "enc"],
                    "description": "Device type: auto (best available), sd (SD Express), usb (USB storage), enc (NVMe enclosure)",
                    "default": "auto",
                },
                "device_index": {
                    "type": "integer",
                    "description": "Index of device to use (from scan results)",
                    "default": 0,
                },
            },
            "required": [],
        },
    },
    {
        "name": "vram_deactivate",
        "description": "Safely stop VRAM expansion and release all resources.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "vram_status",
        "description": (
            "Get real-time VRAM expansion status: memory pool utilization, "
            "transfer stats, prefetch hit rate, device health, active alerts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "vram_estimate",
        "description": (
            "Estimate performance for a specific LLM model on current hardware. "
            "Returns: tokens/s, context window size, feasibility rating, tier distribution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "enum": ["llama3_8b_q4", "llama3_8b_q8", "llama3_70b_q4", "llama3_70b_fp16"],
                    "description": "Model profile key",
                    "default": "llama3_70b_q4",
                },
            },
            "required": [],
        },
    },
    {
        "name": "vram_allocate",
        "description": "Allocate a memory block in the VRAM expansion pool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "block_id": {
                    "type": "string",
                    "description": "Unique block identifier (e.g., 'kv_cache_0', 'layer_42_weight')",
                },
                "size_mb": {
                    "type": "number",
                    "description": "Size in megabytes",
                },
                "tag": {
                    "type": "string",
                    "description": "Usage tag for tracking (e.g., 'weight', 'kv_cache', 'activation')",
                    "default": "",
                },
            },
            "required": ["block_id", "size_mb"],
        },
    },
    {
        "name": "vram_free",
        "description": "Free a previously allocated memory block.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "block_id": {
                    "type": "string",
                    "description": "Block identifier to free",
                },
            },
            "required": ["block_id"],
        },
    },
    {
        "name": "vram_health",
        "description": (
            "Get health status of all monitored devices: temperature, wear level, "
            "error counts, connection status, recent alerts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "vram_audit",
        "description": "Query recent audit log entries for VRAM operations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return",
                    "default": 20,
                },
                "operation": {
                    "type": "string",
                    "description": "Filter by operation type (allocate, free, migrate, transfer)",
                    "default": "",
                },
            },
            "required": [],
        },
    },
]


class VRAMBoosterMCPServer:
    """
    MCP Server 實作。
    透過 stdin/stdout 接收 JSON-RPC 2.0 訊息。
    """

    def __init__(self):
        self._system = None       # 當前活動的微系統
        self._system_type = None  # "sd" / "usb" / "enc"
        self._scan_results = {}

    # ── JSON-RPC Dispatch ──

    def handle_request(self, request: Dict) -> Dict:
        """處理單一 JSON-RPC 請求"""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                result = self._handle_tool_call(params)
            elif method == "notifications/initialized":
                return {}  # 無需回應
            elif method == "ping":
                result = {}
            else:
                return self._error_response(req_id, -32601, f"Method not found: {method}")

            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        except Exception as e:
            logger.error("Request error: %s", e)
            return self._error_response(req_id, -32603, str(e))

    def _handle_initialize(self, params: Dict) -> Dict:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "vram-booster",
                "version": "0.2.0",
            },
        }

    def _handle_tool_call(self, params: Dict) -> Dict:
        name = params.get("name", "")
        args = params.get("arguments", {})

        handler_map = {
            "vram_scan": self._tool_scan,
            "vram_activate": self._tool_activate,
            "vram_deactivate": self._tool_deactivate,
            "vram_status": self._tool_status,
            "vram_estimate": self._tool_estimate,
            "vram_allocate": self._tool_allocate,
            "vram_free": self._tool_free,
            "vram_health": self._tool_health,
            "vram_audit": self._tool_audit,
        }

        handler = handler_map.get(name)
        if not handler:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

        try:
            result = handler(args)
            text = json.dumps(result, indent=2, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}

    # ── Tool Implementations ──

    def _tool_scan(self, args: Dict) -> Dict:
        from .systems.sd_vram_system import SDVRAMSystem
        from .systems.usb_vram_system import USBVRAMSystem
        from .systems.enc_vram_system import EnclosureVRAMSystem

        results = {}
        for name, cls in [("sd", SDVRAMSystem), ("usb", USBVRAMSystem), ("enc", EnclosureVRAMSystem)]:
            sys_instance = cls()
            results[name] = sys_instance.scan()

        self._scan_results = results
        return results

    def _tool_activate(self, args: Dict) -> Dict:
        device_type = args.get("device_type", "auto")
        device_index = args.get("device_index", 0)

        from .systems.sd_vram_system import SDVRAMSystem
        from .systems.usb_vram_system import USBVRAMSystem
        from .systems.enc_vram_system import EnclosureVRAMSystem

        if device_type == "auto":
            # 優先：Enclosure > SD > USB
            for dtype, cls in [("enc", EnclosureVRAMSystem), ("sd", SDVRAMSystem), ("usb", USBVRAMSystem)]:
                sys_instance = cls()
                scan = sys_instance.scan()
                key = {"enc": "enclosure_devices_found", "sd": "sd_devices_found", "usb": "usb_devices_found"}[dtype]
                if scan.get(key, 0) > 0:
                    self._system = sys_instance
                    self._system_type = dtype
                    break
        else:
            cls_map = {"sd": SDVRAMSystem, "usb": USBVRAMSystem, "enc": EnclosureVRAMSystem}
            cls = cls_map.get(device_type)
            if not cls:
                return {"error": f"Unknown device type: {device_type}"}
            self._system = cls()
            self._system.scan()
            self._system_type = device_type

        if self._system is None:
            return {"error": "No compatible devices found", "success": False}

        success = self._system.activate(device_index)
        return {"success": success, "system": self._system_type, "status": self._system.status() if success else {}}

    def _tool_deactivate(self, args: Dict) -> Dict:
        if self._system:
            self._system.deactivate()
            name = self._system_type
            self._system = None
            self._system_type = None
            return {"success": True, "deactivated": name}
        return {"success": False, "reason": "No active system"}

    def _tool_status(self, args: Dict) -> Dict:
        if self._system:
            return self._system.status()
        return {"active": False, "reason": "No active system. Call vram_activate first."}

    def _tool_estimate(self, args: Dict) -> Dict:
        model = args.get("model", "llama3_70b_q4")

        if self._system:
            est = self._system.estimate_performance(model)
            return vars(est) if hasattr(est, '__dict__') else {"result": str(est)}

        # 無活動系統時，建立臨時系統做預估
        from .systems.sd_vram_system import SDVRAMSystem
        sys_instance = SDVRAMSystem()
        sys_instance.scan()
        est = sys_instance.estimate_performance(model)
        return vars(est) if hasattr(est, '__dict__') else {"result": str(est)}

    def _tool_allocate(self, args: Dict) -> Dict:
        if not self._system or not self._system._is_active:
            return {"error": "System not active. Call vram_activate first."}

        block_id = args.get("block_id", "")
        size_mb = args.get("size_mb", 0)
        tag = args.get("tag", "")

        if not block_id or size_mb <= 0:
            return {"error": "block_id and size_mb > 0 required"}

        pool = self._system._pool
        if pool is None:
            return {"error": "Memory pool not initialized"}

        try:
            tier = pool.allocate(block_id, int(size_mb * 1024 * 1024), tag=tag)
            return {"success": True, "block_id": block_id, "tier": tier.name, "size_mb": size_mb}
        except (MemoryError, ValueError) as e:
            return {"success": False, "error": str(e)}

    def _tool_free(self, args: Dict) -> Dict:
        if not self._system or not self._system._is_active:
            return {"error": "System not active"}

        block_id = args.get("block_id", "")
        pool = self._system._pool
        if pool:
            pool.free(block_id)
            return {"success": True, "freed": block_id}
        return {"error": "Memory pool not initialized"}

    def _tool_health(self, args: Dict) -> Dict:
        if self._system and hasattr(self._system, '_monitor') and self._system._monitor:
            metrics = self._system._monitor.get_all_metrics()
            alerts = self._system._monitor.get_alerts(10)
            return {
                "overall": self._system._monitor.get_overall_health().value,
                "devices": {k: vars(v) for k, v in metrics.items()},
                "recent_alerts": [{"level": a.level.value, "message": a.message} for a in alerts],
            }
        return {"overall": "unknown", "reason": "No active monitor"}

    def _tool_audit(self, args: Dict) -> Dict:
        limit = args.get("limit", 20)
        operation = args.get("operation", "")

        try:
            from .core.audit_log import AuditLog
            log = AuditLog.get_instance()
            entries = log.query(limit=limit, operation=operation)
            return {"entries": entries, "count": len(entries)}
        except Exception:
            return {"entries": [], "count": 0, "note": "Audit log not available"}

    # ── Helpers ──

    @staticmethod
    def _error_response(req_id: Any, code: int, message: str) -> Dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def run_stdio_server() -> None:
    """以 stdio 模式運行 MCP Server"""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    server = VRAMBoosterMCPServer()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        response = server.handle_request(request)
        if response:  # notifications don't get responses
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
