from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


PLUGIN_NAME = "astrbot_plugin_gpu_price_watch"
WATCHER_DIR = Path("/AstrBot/data/gpu-price-watcher-browser")
ENV_PUSH_ORIGIN = os.environ.get("GPU_WATCH_PUSH_ORIGIN", "").strip()
# Set GPU_WATCH_PUSH_ORIGIN to a unified_msg_origin such as
# "weixin_personal:FriendMessage:<YOUR_SESSION_ID>@im.wechat" to pre-bind the
# reminder target without running /gpuwatch_bind in a real chat session.
WEIXIN_PLATFORM_IDS = {"weixin_personal"}
WATCHER_CMD = (
    "cd /AstrBot/data/gpu-price-watcher-browser && "
    "flock -n /tmp/browser-market-watcher.lock "
    "xvfb-run -a python browser_market_watcher.py --config browser_market_config.json --verbose"
)


@register("gpu_price_watch", "Xmage-x", "GPU 价格监控主动推送插件", "0.1.0")
class GpuPriceWatchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.interval_seconds = int(os.environ.get("GPU_WATCH_INTERVAL_SECONDS", "900"))
        self._task: asyncio.Task | None = None
        self._running = False
        self.state = self._load_state()

    async def initialize(self):
        self._task = asyncio.create_task(self._loop(), name="gpu-price-watch-loop")
        asyncio.create_task(self._startup_notice_once(), name="gpu-price-watch-startup-notice")
        logger.info("[GPU价格监控] 后台巡检任务已启动")

    async def terminate(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        logger.info("[GPU价格监控] 后台巡检任务已停止")

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"origins": [], "last_run": None, "last_summary": "尚未运行"}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[GPU价格监控] 读取状态失败: {exc}")
            return {"origins": [], "last_run": None, "last_summary": "状态文件读取失败"}

    def _save_state(self):
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _origin_platform(origin: str) -> str:
        return str(origin or "").split(":", 1)[0]

    @classmethod
    def _is_weixin_origin(cls, origin: str) -> bool:
        return cls._origin_platform(origin) in WEIXIN_PLATFORM_IDS

    @staticmethod
    def _is_webchat_origin(origin: str) -> bool:
        return str(origin or "").startswith("webchat:")

    @staticmethod
    def _event_origin(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    def _set_fixed_origin(self, origin: str):
        self.state["fixed_weixin_origin"] = origin
        self.state["origins"] = [origin]
        self.state["last_push_error"] = ""
        self._save_state()

    def _bound_origins(self) -> list[str]:
        if ENV_PUSH_ORIGIN:
            return [ENV_PUSH_ORIGIN]
        fixed = str(self.state.get("fixed_weixin_origin") or "").strip()
        if fixed:
            return [fixed]
        origins = [
            str(origin).strip()
            for origin in self.state.get("origins", [])
            if self._is_weixin_origin(str(origin).strip())
        ]
        if origins:
            self.state["fixed_weixin_origin"] = origins[0]
            self.state["origins"] = [origins[0]]
            return [origins[0]]
        return []

    async def _send_text(self, text: str) -> tuple[int, list[str]]:
        chain = MessageChain([Plain(text)])
        sent_count = 0
        errors: list[str] = []
        for origin in self._bound_origins():
            try:
                matched = await self.context.send_message(origin, chain)
                if matched:
                    sent_count += 1
                    logger.info(f"[GPU价格监控] 已向固定微信会话提交消息: {origin}")
                else:
                    message = f"找不到固定微信平台，消息未发送: {origin}"
                    errors.append(message)
                    logger.warning(f"[GPU价格监控] {message}")
            except Exception as exc:
                message = f"推送失败 {origin}: {exc}"
                errors.append(message)
                logger.warning(f"[GPU价格监控] {message}")
        if errors:
            self.state["last_push_error"] = " | ".join(errors)[-1200:]
        elif sent_count > 0:
            self.state["last_push_error"] = ""
        self._save_state()
        return sent_count, errors

    async def _startup_notice_once(self):
        await asyncio.sleep(45)
        if not self._bound_origins() or self.state.get("startup_notice_sent"):
            return
        sent_count, _errors = await self._send_text(
            "GPU 价格监控插件已接入 AstrBot 微信推送。\n"
            "可用命令: /gpuwatch_status /gpuwatch_now /gpuwatch_test"
        )
        if sent_count <= 0:
            return
        self.state["startup_notice_sent"] = True
        self._save_state()

    async def _run_watcher(self) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_shell(
            WATCHER_CMD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return 124, "", "巡检超时，已停止本轮浏览器任务"
        return (
            int(proc.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _format_output(stdout: str, stderr: str) -> tuple[str, bool]:
        lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
        stats = [line for line in lines if " | " in line and "candidates=" in line]
        alert_index = next(
            (idx for idx, line in enumerate(lines) if "显卡搜索结果提醒" in line),
            -1,
        )
        has_alert = alert_index >= 0
        if has_alert:
            alert_lines = lines[alert_index:]
            body = "\n".join(alert_lines[:80])
            if len(alert_lines) > 80:
                body += f"\n...已截断，还有 {len(alert_lines) - 80} 行可在日志中查看"
            return body, True
        if stderr.strip():
            return "GPU 巡检没有产生提醒，但有错误输出:\n" + stderr.strip()[:1200], False
        if stats:
            return "GPU 巡检完成，本轮没有达到提醒条件:\n" + "\n".join(stats[:12]), False
        return "GPU 巡检完成，本轮没有达到提醒条件。", False

    async def _check_once(self, manual: bool = False) -> str:
        if self._running:
            return "GPU 巡检正在运行，本轮先跳过，避免小主机过载。"
        if not WATCHER_DIR.exists():
            return f"巡检目录不存在: {WATCHER_DIR}"

        self._running = True
        started = int(time.time())
        try:
            code, stdout, stderr = await self._run_watcher()
            message, has_alert = self._format_output(stdout, stderr)
            self.state["last_run"] = started
            self.state["last_summary"] = message[:500]
            self.state["last_exit_code"] = code
            self._save_state()

            if has_alert:
                sent_count, _errors = await self._send_text(message)
                if sent_count <= 0:
                    logger.warning("[GPU价格监控] 命中提醒但固定微信推送失败")
            elif manual:
                return message
            elif code != 0 and stderr.strip():
                await self._send_text("GPU 巡检异常:\n" + stderr.strip()[:1200])
            return message
        finally:
            self._running = False

    async def _loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                if self._bound_origins():
                    await self._check_once(manual=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[GPU价格监控] 后台巡检异常: {exc}")
            await asyncio.sleep(self.interval_seconds)

    @filter.command("gpuwatch_bind")
    async def bind(self, event: AstrMessageEvent):
        """绑定当前会话为 GPU 价格提醒接收方。"""
        origin = self._event_origin(event)
        if not origin:
            yield event.plain_result("没有拿到当前会话 ID，绑定失败。")
            return
        if self._is_webchat_origin(origin):
            yield event.plain_result("这个命令请在真正的微信聊天里发送，网页后端会话不能作为微信推送目标。")
            return
        if not self._is_weixin_origin(origin):
            yield event.plain_result(f"当前平台不是已知微信机器人平台，未绑定: {origin}")
            return
        self._set_fixed_origin(origin)
        yield event.plain_result(f"已固定推送到当前微信会话: {origin}")

    @filter.command("gpuwatch_test")
    async def test(self, event: AstrMessageEvent):
        """发送 GPU 价格监控测试消息。"""
        origin = self._event_origin(event)
        if origin and self._is_weixin_origin(origin):
            self._set_fixed_origin(origin)
        if not self._bound_origins():
            yield event.plain_result(
                "还没有绑定真实微信会话。请在要接收提醒的微信聊天里发送 /gpuwatch_bind，"
                "然后再发送 /gpuwatch_test。"
            )
            return
        sent_count, errors = await self._send_text("GPU 价格监控测试消息：如果你在微信里看见这条，主动推送链路就是通的。")
        if sent_count > 0:
            yield event.plain_result(f"测试消息已提交到固定微信会话: {self._bound_origins()[0]}")
        else:
            detail = "\n".join(errors[-3:]) if errors else "没有返回更具体的错误。"
            yield event.plain_result(
                "测试消息没有发出去。底层 weixin_oc 主动发送失败。\n"
                f"{detail}\n"
                "请在真正的微信聊天里先发 /gpuwatch_bind 刷新会话，再发 /gpuwatch_test。"
            )

    @filter.command("gpuwatch_now")
    async def run_now(self, event: AstrMessageEvent):
        """立即运行一次 GPU 价格巡检。"""
        yield event.plain_result("开始运行 GPU 价格巡检，本轮可能需要 2 到 4 分钟。")
        message = await self._check_once(manual=True)
        yield event.plain_result(message)

    @filter.command("gpuwatch_status")
    async def status(self, event: AstrMessageEvent):
        """查看 GPU 价格监控状态。"""
        origins = self._bound_origins()
        last_run = self.state.get("last_run")
        last_text = "尚未运行"
        if last_run:
            last_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(last_run)))
        summary = str(self.state.get("last_summary") or "无")
        push_error = str(self.state.get("last_push_error") or "无")
        yield event.plain_result(
            "GPU 价格监控状态\n"
            f"绑定会话数: {len(origins)}\n"
            f"固定目标: {origins[0] if origins else '未绑定真实微信会话'}\n"
            f"巡检间隔: {self.interval_seconds // 60} 分钟\n"
            f"正在运行: {'是' if self._running else '否'}\n"
            f"上次运行: {last_text}\n"
            f"上次推送错误: {push_error[:300]}\n"
            f"上次摘要: {summary[:300]}"
        )
