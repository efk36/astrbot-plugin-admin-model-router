import asyncio
import time

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

SESSION_PROVIDER_KEY = "provider_perf_chat_completion"


class AdminModelRouterPlugin(Star):
    """按发送者身份路由到不同的对话模型。

    管理员 -> 会话内已指定的模型优先，否则用配置里指定的 provider
    普通用户 -> AstrBot 自身配置的默认 provider

    目标 provider 不可用时，先按可配置次数重试探测，仍失败则回落到默认模型。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._warned: set[str] = set()
        # provider_id -> 探测成功的时间戳，用于避免每条消息都探测一次
        self._probe_ok_at: dict[str, float] = {}

    # ---------- 配置读取 ----------

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _admin_provider_id(self) -> str:
        return str(self.config.get("admin_provider_id", "") or "").strip()

    def _extra_admin_ids(self) -> set[str]:
        return {
            str(u).strip()
            for u in (self.config.get("extra_admin_ids") or [])
            if str(u).strip()
        }

    def _respect_session_model(self) -> bool:
        return bool(self.config.get("respect_session_model", True))

    def _max_retries(self) -> int:
        try:
            return max(1, int(self.config.get("max_retries", 2)))
        except (TypeError, ValueError):
            return 2

    def _retry_delay(self) -> float:
        try:
            return max(0.0, float(self.config.get("retry_delay_s", 1.5)))
        except (TypeError, ValueError):
            return 1.5

    def _probe_timeout(self) -> float:
        try:
            return max(1.0, float(self.config.get("probe_timeout_s", 20)))
        except (TypeError, ValueError):
            return 20.0

    def _probe_prompt(self) -> str:
        return str(self.config.get("probe_prompt", "hi") or "hi")

    def _probe_enabled(self) -> bool:
        return bool(self.config.get("probe_before_switch", True))

    def _probe_cache(self) -> float:
        try:
            return max(0.0, float(self.config.get("probe_cache_s", 300)))
        except (TypeError, ValueError):
            return 300.0

    def _notify_fallback(self) -> bool:
        return bool(self.config.get("notify_on_fallback", True))

    def _log_switch(self) -> bool:
        return bool(self.config.get("log_switch", True))

    # ---------- 身份判断 ----------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return event.get_sender_id() in self._extra_admin_ids()

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message, *args)

    def _available_providers(self) -> str:
        """列出当前所有可用的对话 provider ID -> 模型名，方便排查填错。"""
        try:
            providers = self.context.get_all_providers()
        except Exception:
            return "<无法获取 provider 列表>"
        items = []
        for p in providers or []:
            provider_id = str(p.provider_config.get("id", "?")) if getattr(p, "provider_config", None) else "?"
            model = p.get_model() if hasattr(p, "get_model") else "?"
            items.append(f"{provider_id} -> {model}")
        return "\n".join(items) if items else "<没有可用的对话 provider>"

    # ---------- 会话内已指定的模型 ----------

    async def _session_provider_id(self, event: AstrMessageEvent) -> str:
        """读取 AstrBot 为该会话保存的 provider 偏好。

        在 WebUI 会话管理里切过模型，或用指令改过模型后，偏好会写进
        umo 作用域的 provider_perf_chat_completion。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return ""
        try:
            value = await sp.get_async("umo", umo, SESSION_PROVIDER_KEY, None)
        except Exception:
            logger.debug("[admin_model_router] 读取会话 provider 偏好失败", exc_info=True)
            return ""
        return str(value or "").strip()

    async def _resolve_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        """决定管理员该用哪个 provider。

        返回 (provider_id, 来源标记)。来源标记用于日志和 /模型路由 输出。
        """
        if self._respect_session_model():
            session_id = await self._session_provider_id(event)
            if session_id:
                return session_id, "session"
        return self._admin_provider_id(), "config"

    # ---------- 可用性探测 ----------

    async def _probe(self, provider_id: str, provider) -> bool:
        """用最小请求探测指定 provider 是否真的可用，失败按配置重试。"""
        cache = self._probe_cache()
        now = time.monotonic()
        last_ok = self._probe_ok_at.get(provider_id)
        if last_ok is not None and now - last_ok < cache:
            return True

        attempts = self._max_retries()
        delay = self._retry_delay()
        timeout = self._probe_timeout()
        prompt = self._probe_prompt()
        last_error = ""

        for attempt in range(1, attempts + 1):
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt),
                    timeout=timeout,
                )
                if getattr(resp, "role", "") == "err":
                    last_error = str(getattr(resp, "completion_text", "")) or "err response"
                else:
                    self._probe_ok_at[provider_id] = time.monotonic()
                    if self._log_switch():
                        logger.info(
                            "[admin_model_router] probe ok on attempt %d/%d",
                            attempt,
                            attempts,
                        )
                    return True
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < attempts and delay:
                await asyncio.sleep(delay)

        self._probe_ok_at.pop(provider_id, None)
        logger.error(
            "[admin_model_router] provider `%s` unavailable after %d attempt(s): %s",
            provider_id,
            attempts,
            last_error,
        )
        return False

    # ---------- 路由入口 ----------

    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent) -> None:
        """在 AstrBot 解析 provider 之前改写路由。

        普通用户不设置 selected_provider，AstrBot 会照常使用自己配置的默认模型。
        """
        if not self._enabled():
            return

        # 普通用户：保持 AstrBot 的默认模型，不做任何干预。
        if not self._is_admin(event):
            return

        # 已经有明确的 provider 选择（其他插件或 AstrBot 自身设的）就不抢。
        existing = event.get_extra("selected_provider")
        if isinstance(existing, str) and existing.strip():
            if self._log_switch():
                logger.info(
                    "[admin_model_router] selected_provider already set to `%s`, skip",
                    existing,
                )
            return

        provider_id, source = await self._resolve_target(event)
        if not provider_id:
            self._warn_once(
                "no_provider_id",
                "[admin_model_router] admin_provider_id 未配置，管理员消息将使用默认模型。",
            )
            return

        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            self._warn_once(
                f"missing:{provider_id}",
                "[admin_model_router] 找不到 provider `%s`，管理员消息将使用默认模型。\n当前可用的 provider ID -> 模型名：\n%s",
                provider_id,
                self._available_providers(),
            )
            return

        if not hasattr(provider, "text_chat"):
            self._warn_once(
                f"not_chat:{provider_id}",
                "[admin_model_router] provider `%s` 不是对话模型，管理员消息将使用默认模型。",
                provider_id,
            )
            return

        if self._probe_enabled() and not await self._probe(provider_id, provider):
            if self._notify_fallback():
                await self._send(event, "指定模型暂时不可用，已改用默认模型回复。")
            return

        event.set_extra("selected_provider", provider_id)
        if self._log_switch():
            logger.info(
                "[admin_model_router] sender=%s routed to provider `%s` (source=%s)",
                event.get_sender_id(),
                provider_id,
                source,
            )

    # ---------- 发送封装 ----------

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        try:
            # send() 只接受 MessageChain，直接传字符串会抛
            # AttributeError: 'str' object has no attribute 'chain'
            await event.send(event.plain_result(text))
        except Exception:
            logger.warning("[admin_model_router] 发送提示失败", exc_info=True)

    # ---------- 命令 ----------

    @filter.command("模型路由")
    async def show_router(self, event: AstrMessageEvent):
        """查看当前模型路由配置。仅管理员可用。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以查看模型路由配置。")
            return

        target_id, source = await self._resolve_target(event)
        source_label = "会话内指定的模型" if source == "session" else "插件配置 admin_provider_id"

        if not target_id:
            yield event.plain_result(
                "模型路由已启用，但未配置 admin_provider_id，当前所有用户都走默认模型。"
            )
            return

        provider = self.context.get_provider_by_id(target_id)
        if provider is None or not hasattr(provider, "text_chat"):
            yield event.plain_result(
                f"模型路由已启用，但 provider `{target_id}` 不存在或不是对话模型，"
                "管理员消息当前会回落到默认模型。\n\n"
                "当前可用的 provider ID -> 模型名：\n" + self._available_providers()
            )
            return

        model = provider.get_model() if hasattr(provider, "get_model") else "未知"
        lines = [
            "模型路由配置：",
            f"- 状态：{'启用' if self._enabled() else '停用'}",
            f"- 管理员模型：{target_id}（{model}）",
            f"- 选择来源：{source_label}",
            f"- 跟随会话内模型：{'是' if self._respect_session_model() else '否'}",
            "- 普通用户：AstrBot 默认模型",
            f"- 探测重试：{self._max_retries()} 次，间隔 {self._retry_delay()} 秒",
            f"- 探测超时：{self._probe_timeout()} 秒",
            f"- 成功缓存：{self._probe_cache()} 秒",
        ]
        yield event.plain_result("\n".join(lines))