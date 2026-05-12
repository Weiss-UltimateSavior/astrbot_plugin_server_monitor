# ============================================================
# AstrBot 服务器状态监控插件
# 功能: ICMP Ping 服务器监控 + HTTP Web 服务健康检查 + 到期提醒 + 每日报告
# 版本: 1.1.0
# ============================================================

import asyncio
import json
import os
import datetime
import ssl
from typing import Dict, List, Optional
from dataclasses import dataclass

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Image

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


# ------------------------------------------------------------
# 数据模型
# ------------------------------------------------------------

@dataclass
class ServerInfo:
    """服务器监控信息"""
    name: str                                          # 服务器名称
    ip: str                                            # IP 地址或域名
    announcement: str                                  # 在线公告文本
    expiry_date: Optional[str]                         # 到期日期 (YYYY-MM-DD)
    is_online: bool = False                            # 当前是否在线
    last_check_time: Optional[datetime.datetime] = None  # 最后检测时间
    last_online_time: Optional[datetime.datetime] = None  # 最后在线时间 (用于判断是否曾经在线)
    consecutive_offline_count: int = 0                 # 连续离线次数


@dataclass
class WebCheckInfo:
    """Web 服务健康检查信息"""
    name: str                                          # 检查项名称
    url: str                                           # 检查 URL
    method: str = "GET"                                # HTTP 方法
    expected_code: int = 200                           # 期望的 HTTP 状态码
    timeout: int = 5                                   # 超时秒数
    host_header: str = ""                              # 自定义 Host 头 (内网/反向代理场景)
    must_contain: str = ""                             # 响应体应包含的关键字 (限前 64KB)
    verify_ssl: bool = True                            # 是否验证 SSL 证书
    allow_redirects: bool = True                       # 是否跟随重定向
    is_healthy: bool = True                            # 当前是否健康
    last_check_time: Optional[datetime.datetime] = None  # 最后检查时间
    consecutive_fail_count: int = 0                    # 连续失败次数


# ------------------------------------------------------------
# 插件主体
# ------------------------------------------------------------

@register("server_monitor", "服务器状态监控", "监控服务器在线状态，支持到期提醒和Web服务检查", "1.1.0")
class ServerMonitorPlugin(Star):
    """
    服务器监控插件

    监控周期: 每隔 ping_interval 秒对所有服务器执行 Ping，
    对所有 Web 检查项执行 HTTP 健康检查，同时检查到期状态。
    状态变更时发送通知，每日定时发送文字 + 图片报告。
    """

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # ---- 配置项 ----
        self.servers: Dict[str, ServerInfo] = {}               # ip -> ServerInfo
        self.web_checks: Dict[str, WebCheckInfo] = {}          # url -> WebCheckInfo
        self.target_groups: List[str] = config.get("target_groups", [])
        self.ping_interval: int = config.get("ping_interval", 10)
        self.expiry_notify_days: int = config.get("expiry_notify_days", 1)
        self.offline_notify_enabled: bool = config.get("offline_notify_enabled", True)
        self.online_notify_enabled: bool = config.get("online_notify_enabled", True)
        self.daily_report_time: str = config.get("daily_report_time", "08:00")
        self.ping_timeout: int = config.get("ping_timeout", 3)
        self.max_concurrent_checks: int = config.get("max_concurrent_checks", 5)

        # ---- 调度器和任务 ----
        self.scheduler = AsyncIOScheduler()
        self._monitor_task = None                               # 监控循环 asyncio.Task

        # ---- 通知去重集合 (key 格式见各方法) ----
        self._notified_expiry: set = set()                      # 到期通知去重
        self._notified_offline: set = set()                     # 服务器离线通知去重 (按 IP)
        self._notified_web_fail: set = set()                    # Web 服务异常通知去重 (按 URL)

        # ---- 持久化文件路径 ----
        self._data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers_data.json")

        # ---- UMO 缓存: 用于主动消息发送 ----
        # 不同平台/版本的 unified_msg_origin 格式不同，通过 /绑定通知群 指令从真实事件中学习
        self._umo_cache: Dict[str, str] = {}                    # 配置群组字符串 -> 真实 unified_msg_origin

        # ---- 并发控制: 限制同时发起的 HTTP 检查数，避免打满连接池 ----
        self._web_check_sem = asyncio.Semaphore(self.max_concurrent_checks)

        # ---- 初始化: 加载数据 -> 验证配置 -> 启动监控 ----
        self._load_servers()
        self._load_web_checks()
        self._validate_target_groups()

        if self.target_groups:
            self._start_monitor()
            self._setup_daily_report()
            logger.info(f"服务器监控插件已启动，监控 {len(self.servers)} 台服务器 {len(self.web_checks)} 个Web服务")
        else:
            logger.warning("服务器监控插件：未配置目标群组，监控功能未启动")

    # ================================================================
    # 配置验证与数据加载
    # ================================================================

    def _validate_target_groups(self):
        """验证 target_groups 格式: 平台名称:GroupMessage:群号"""
        valid_groups = []
        for group_id in self.target_groups:
            parts = group_id.split(":")
            if len(parts) != 3:
                logger.error(f"服务器监控插件：群组格式错误 '{group_id}'，正确格式应为: 平台名称:GroupMessage:群号")
                continue
            valid_groups.append(group_id)
        self.target_groups = valid_groups
        if valid_groups:
            logger.info(f"服务器监控插件：已验证 {len(valid_groups)} 个有效群组")

    def _load_servers(self):
        """
        加载服务器配置，合并来源: 持久化文件 + 插件配置
        持久化文件由 /添加服务器 /删除服务器 指令维护
        插件配置来自 WebUI _conf_schema.json
        合并策略: 配置中的同名 IP 会覆盖持久化文件中的公告/到期日期字段
        """
        loaded_ips = set()

        # 1. 从持久化文件加载
        if os.path.exists(self._data_file):
            try:
                with open(self._data_file, "r", encoding="utf-8") as f:
                    persisted = json.load(f)
                for srv in persisted.get("servers", []):
                    ip = srv.get("ip", "")
                    if not ip:
                        continue
                    self.servers[ip] = ServerInfo(
                        name=srv.get("name", ip),
                        ip=ip,
                        announcement=srv.get("announcement", ""),
                        expiry_date=srv.get("expiry_date"),
                    )
                    loaded_ips.add(ip)
                logger.info(f"从持久化文件加载了 {len(persisted.get('servers', []))} 台服务器配置")
            except (json.JSONDecodeError, FileNotFoundError, IOError) as e:
                logger.warning(f"加载持久化服务器数据失败: {e}")

        # 2. 从插件配置加载 (WebUI 配置优先)
        servers_json = self.config.get("servers", "[]")
        try:
            servers_list = json.loads(servers_json)
            for srv in servers_list:
                name = srv.get("name", srv.get("ip", "未命名"))
                ip = srv.get("ip", "")
                if not ip:
                    continue
                if ip in loaded_ips:
                    # 已从持久化加载，用配置覆盖部分字段
                    config_server = self.servers[ip]
                    if srv.get("name"):
                        config_server.name = name
                    if srv.get("announcement"):
                        config_server.announcement = srv.get("announcement", "")
                    if srv.get("expiry_date"):
                        config_server.expiry_date = srv.get("expiry_date")
                else:
                    self.servers[ip] = ServerInfo(
                        name=name,
                        ip=ip,
                        announcement=srv.get("announcement", ""),
                        expiry_date=srv.get("expiry_date"),
                    )
                loaded_ips.add(ip)
            logger.info(f"从配置加载了 {len(servers_list)} 台服务器")
        except json.JSONDecodeError as e:
            logger.error(f"服务器配置解析失败: {e}")

        logger.info(f"共加载 {len(self.servers)} 台服务器配置")

    def _load_web_checks(self):
        """
        加载 Web 服务健康检查配置，合并来源: 持久化文件 + 插件配置
        与 _load_servers 同理，配置中的同 URL 项会跳过 (持久化优先用于动态添加)
        """
        # 1. 从持久化文件加载
        if os.path.exists(self._data_file):
            try:
                with open(self._data_file, "r", encoding="utf-8") as f:
                    persisted = json.load(f)
                for check in persisted.get("web_checks", []):
                    url = check.get("url", "")
                    if not url:
                        continue
                    self.web_checks[url] = WebCheckInfo(
                        name=check.get("name", url),
                        url=url,
                        method=check.get("method", "GET"),
                        expected_code=check.get("expected_code", 200),
                        timeout=check.get("timeout", 5),
                        host_header=check.get("host_header", ""),
                        must_contain=check.get("must_contain", ""),
                        verify_ssl=check.get("verify_ssl", True),
                        allow_redirects=check.get("allow_redirects", True),
                    )
            except (json.JSONDecodeError, FileNotFoundError, IOError) as e:
                logger.warning(f"加载持久化Web检查数据失败: {e}")

        # 2. 从插件配置加载
        web_checks_json = self.config.get("web_checks", "[]")
        try:
            checks_list = json.loads(web_checks_json)
            for check in checks_list:
                url = check.get("url", "")
                if not url:
                    continue
                # 配置不覆盖已持久化的同 URL 项 (支持动态添加)
                if url not in self.web_checks:
                    self.web_checks[url] = WebCheckInfo(
                        name=check.get("name", url),
                        url=url,
                        method=check.get("method", "GET"),
                        expected_code=check.get("expected_code", 200),
                        timeout=check.get("timeout", 5),
                        host_header=check.get("host_header", ""),
                        must_contain=check.get("must_contain", ""),
                        verify_ssl=check.get("verify_ssl", True),
                        allow_redirects=check.get("allow_redirects", True),
                    )
            logger.info(f"从配置加载了 {len(checks_list)} 个Web检查项")
        except json.JSONDecodeError as e:
            logger.error(f"Web检查配置解析失败: {e}")

        logger.info(f"共加载 {len(self.web_checks)} 个Web检查项")

    def _save_all_data(self):
        """持久化所有数据 (服务器 + Web 检查) 到 servers_data.json"""
        data = {
            "servers": [],
            "web_checks": [],
        }
        for server in self.servers.values():
            data["servers"].append({
                "name": server.name,
                "ip": server.ip,
                "announcement": server.announcement,
                "expiry_date": server.expiry_date,
            })
        for check in self.web_checks.values():
            data["web_checks"].append({
                "name": check.name,
                "url": check.url,
                "method": check.method,
                "expected_code": check.expected_code,
                "timeout": check.timeout,
                "host_header": check.host_header,
                "must_contain": check.must_contain,
                "verify_ssl": check.verify_ssl,
                "allow_redirects": check.allow_redirects,
            })
        try:
            with open(self._data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存数据失败: {e}")

    # ================================================================
    # 工具方法
    # ================================================================

    def _get_days_left(self, expiry_date_str: str) -> Optional[int]:
        """计算距离到期日还有多少天，返回负数表示已过期，None 表示解析失败"""
        if not expiry_date_str:
            return None
        try:
            expiry = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
            return (expiry - datetime.date.today()).days
        except ValueError as e:
            logger.warning(f"解析到期日期失败 '{expiry_date_str}': {e}")
            return None

    # ================================================================
    # 消息发送 (主动)
    # ================================================================

    async def _broadcast_message(self, message_chain, log_prefix: str = ""):
        """
        向所有 target_groups 广播消息 (底层方法)
        支持任意 MessageChain (文本/图片)，包含 UMO 缓存和防风控延迟
        """
        if not self.target_groups:
            return
        for group_id in self.target_groups:
            try:
                # 优先使用缓存的真实 unified_msg_origin，否则回退到配置字符串
                umo = self._umo_cache.get(group_id)
                if umo is None:
                    logger.warning(f"群组 {group_id} 尚未捕获 unified_msg_origin，"
                                   "请在目标群发送 /绑定通知群 以完成学习")
                    umo = group_id
                await self.context.send_message(umo, message_chain)
                if log_prefix:
                    logger.info(f"{log_prefix}{group_id}")
                await asyncio.sleep(1)  # 防风控: 每条消息间隔 1 秒
            except Exception as e:
                logger.error(f"发送消息到 {group_id} 失败: {e}")

    async def _send_to_all_groups(self, msg: str, log_prefix: str = ""):
        """发送纯文本消息到所有群组 (便利包装方法)"""
        message_chain = MessageChain().message(msg)
        await self._broadcast_message(message_chain, log_prefix)

    # ================================================================
    # 权限控制
    # ================================================================

    def _get_sender_id(self, event: AstrMessageEvent) -> Optional[str]:
        """
        获取消息发送者 ID，兼容不同平台
        依次尝试: get_sender_id() -> message_obj.sender.user_id -> get_sender_name()
        """
        try:
            return event.get_sender_id()
        except (AttributeError, Exception):
            pass
        try:
            return str(event.message_obj.sender.user_id)
        except (AttributeError, Exception):
            pass
        try:
            return event.get_sender_name()
        except (AttributeError, Exception):
            pass
        return None

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        """
        检查发送者是否有权限使用指令
        如果 admin_users 列表为空，允许所有用户
        否则只允许列表中的用户 ID
        """
        admin_users = self.config.get("admin_users", [])
        if not admin_users:
            return True
        sender_id = self._get_sender_id(event)
        if sender_id is None:
            return False
        return str(sender_id) in [str(u) for u in admin_users]

    # ================================================================
    # UMO 捕获 (消息链路学习)
    # ================================================================

    def _capture_umo(self, event: AstrMessageEvent):
        """
        从事件中捕获真实的 unified_msg_origin 并缓存
        用于解决不同平台/版本下统一消息来源格式不一致的问题
        通过 event.get_group_id() 匹配 target_groups 中配置的群号
        """
        if not self.target_groups:
            return
        try:
            actual_umo = event.unified_msg_origin
        except (AttributeError, Exception):
            return
        try:
            group_id = event.get_group_id()
        except (AttributeError, Exception):
            return
        if not group_id:
            return
        for target in self.target_groups:
            parts = target.split(":")
            # target 格式: 平台名称:GroupMessage:群号
            # 匹配最后的群号 (parts[2]) 与 event 中的 group_id
            if len(parts) == 3 and parts[2] == group_id and target not in self._umo_cache:
                self._umo_cache[target] = actual_umo
                logger.info(f"已捕获群组 {group_id} 的 unified_msg_origin")
                break

    # ================================================================
    # 监控调度
    # ================================================================

    def _start_monitor(self):
        """启动异步监控循环"""
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"服务器监控任务已启动，检测间隔: {self.ping_interval}秒")

    def _setup_daily_report(self):
        """设置每日定时报告 (APScheduler Cron)"""
        if not self.daily_report_time:
            return
        try:
            hour, minute = self.daily_report_time.split(":")
            self.scheduler.add_job(
                self._send_daily_report,
                CronTrigger(hour=int(hour), minute=int(minute)),
                id="daily_report_job",
            )
            self.scheduler.start()
            logger.info(f"每日状态报告已设置: {self.daily_report_time}")
        except Exception as e:
            logger.error(f"设置每日报告失败: {e}")

    async def _monitor_loop(self):
        """
        主监控循环
        每个周期执行: Ping 服务器 -> HTTP 检查 Web 服务 -> 到期检查
        """
        while True:
            try:
                await self._check_all_servers()          # 1. ICMP Ping
                await self._check_all_web_services()     # 2. HTTP 健康检查
                await self._check_expiry_notifications() # 3. 到期提醒
            except asyncio.CancelledError:
                logger.info("监控任务已取消")
                break
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
            await asyncio.sleep(self.ping_interval)

    # ================================================================
    # 服务器 Ping 监控
    # ================================================================

    async def _check_all_servers(self):
        """并发 Ping 所有服务器"""
        tasks = [self._ping_server(ip) for ip in self.servers.keys()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _ping_address(self, ip: str) -> bool:
        """对单个 IP 执行系统 Ping (异步子进程)，返回是否可达"""
        try:
            # Windows 和 Linux/macOS 的 ping 参数不同
            if os.name == 'nt':
                cmd = ['ping', '-n', '1', '-w', str(self.ping_timeout * 1000), ip]
            else:
                cmd = ['ping', '-c', '1', '-W', str(self.ping_timeout), ip]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.ping_timeout + 2  # 额外 2s 缓冲
                )
                return proc.returncode == 0
            except asyncio.TimeoutError:
                return False
        except Exception as e:
            logger.error(f"Ping {ip} 失败: {e}")
            return False

    async def _ping_server(self, ip: str) -> bool:
        """
        Ping 一台服务器并更新其状态
        如果 IP 不在 servers 字典中 (如 /ping 指令)，则仅返回结果不更新状态
        """
        server = self.servers.get(ip)
        if not server:
            return await self._ping_address(ip)

        is_online = await self._ping_address(ip)
        await self._update_server_status(server, is_online)
        return is_online

    async def _update_server_status(self, server: ServerInfo, is_online: bool):
        """
        更新服务器状态并在必要时发送通知
        离线逻辑: 连续 2 次失败才通知 (避免网络瞬时抖动误报)，_notified_offline 去重
        在线逻辑: 只有曾经在线过的服务器恢复时才通知 (避免首次检测就发"恢复在线")
        """
        old_status = server.is_online
        was_ever_online = server.last_online_time is not None  # 在更新 last_online_time 之前捕获
        server.is_online = is_online
        server.last_check_time = datetime.datetime.now()

        if is_online:
            server.last_online_time = datetime.datetime.now()
            server.consecutive_offline_count = 0
            # 只有真正从离线恢复时才通知 (排除首次检测到在线的情况)
            if old_status is False and was_ever_online and self.online_notify_enabled:
                await self._send_status_notification(server, "online")
            # 移除离线去重标记，下次离线可以再次通知
            if server.ip in self._notified_offline:
                self._notified_offline.discard(server.ip)
        else:
            server.consecutive_offline_count += 1
            # 连续 2 次失败 + 未通知过 → 发送离线告警
            if server.consecutive_offline_count >= 2 and self.offline_notify_enabled:
                if server.ip not in self._notified_offline:
                    await self._send_status_notification(server, "offline")
                    self._notified_offline.add(server.ip)

    async def _send_status_notification(self, server: ServerInfo, status: str):
        """构建并发送服务器状态变更通知"""
        if status == "online":
            msg = f"🟢 服务器恢复在线\n\n"
            msg += f"服务器: {server.name}\n"
            msg += f"IP: {server.ip}\n"
            msg += f"恢复时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            if server.announcement:
                msg += f"\n📢 公告: {server.announcement}"
        else:
            msg = f"🔴 服务器离线告警\n\n"
            msg += f"服务器: {server.name}\n"
            msg += f"IP: {server.ip}\n"
            msg += f"离线时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        await self._send_to_all_groups(msg, "已发送状态通知到 ")

    # ================================================================
    # Web 服务 HTTP 健康检查
    # ================================================================

    async def _check_all_web_services(self):
        """并发检查所有 Web 服务"""
        if not self.web_checks:
            return
        tasks = [self._check_single_web_service(url) for url in self.web_checks.keys()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_single_web_service(self, url: str):
        """检查单个 Web 服务并更新状态"""
        check = self.web_checks.get(url)
        if not check:
            return
        is_healthy = await self._http_health_check(check)
        await self._update_web_check_status(check, is_healthy)

    async def _http_health_check(self, check: WebCheckInfo) -> bool:
        """
        对单个 Web 端点执行 HTTP 健康检查
        流程: 通过 Semaphore 控制并发 -> 发 HTTP 请求 -> 检查状态码 -> 可选关键字匹配
        支持: 自定义 Host 头、跳过 SSL 验证 (自签证书)、控制重定向跟随
        安全: must_contain 只检查响应体前 64KB，避免大文件 OOM
        """
        async with self._web_check_sem:  # Semaphore 限制并发 HTTP 连接数
            try:
                timeout = aiohttp.ClientTimeout(total=check.timeout)
                headers = {"User-Agent": "AstrBot-ServerMonitor/1.1"}
                if check.host_header:
                    headers["Host"] = check.host_header

                # 构造 SSL Context (自签证书场景)
                connector = None
                if not check.verify_ssl:
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    connector = aiohttp.TCPConnector(ssl=ssl_context)

                async with aiohttp.ClientSession(
                    timeout=timeout,
                    connector=connector,
                ) as session:
                    async with session.request(
                        check.method,
                        check.url,
                        headers=headers,
                        allow_redirects=check.allow_redirects,
                    ) as resp:
                        # 状态码检查
                        if resp.status != check.expected_code:
                            return False
                        # 关键字匹配 (限 64KB)
                        if check.must_contain:
                            body = await asyncio.wait_for(resp.text(), timeout=check.timeout)
                            body = body[:65536]
                            return check.must_contain in body
                        return True
            except asyncio.TimeoutError:
                logger.debug(f"Web检查 {check.name} 超时: {check.url}")
                return False
            except aiohttp.ClientError as e:
                logger.debug(f"Web检查 {check.name} 网络错误: {e}")
                return False
            except Exception as e:
                logger.error(f"Web检查 {check.name} 异常: {e}")
                return False

    async def _update_web_check_status(self, check: WebCheckInfo, is_healthy: bool):
        """
        更新 Web 检查状态并在必要时发送通知
        逻辑: 连续 2 次失败通知 (与服务器监控一致)，_notified_web_fail 去重
        """
        old_status = check.is_healthy
        check.is_healthy = is_healthy
        check.last_check_time = datetime.datetime.now()

        if is_healthy:
            check.consecutive_fail_count = 0
            # 只有从异常恢复时才通知
            if old_status is False and self.online_notify_enabled:
                await self._send_web_check_notification(check, "recovered")
            if check.url in self._notified_web_fail:
                self._notified_web_fail.discard(check.url)
        else:
            check.consecutive_fail_count += 1
            if check.consecutive_fail_count >= 2 and self.offline_notify_enabled:
                if check.url not in self._notified_web_fail:
                    await self._send_web_check_notification(check, "failed")
                    self._notified_web_fail.add(check.url)

    async def _send_web_check_notification(self, check: WebCheckInfo, status: str):
        """构建并发送 Web 服务状态变更通知"""
        if status == "recovered":
            msg = f"🟢 Web服务恢复\n\n"
            msg += f"服务名称: {check.name}\n"
            msg += f"检查地址: {check.url}\n"
            msg += f"恢复时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            msg = f"🔴 Web服务异常\n\n"
            msg += f"服务名称: {check.name}\n"
            msg += f"检查地址: {check.url}\n"
            msg += f"期望状态码: {check.expected_code}\n"
            msg += f"连续失败: {check.consecutive_fail_count} 次\n"
            msg += f"检测时间: {check.last_check_time.strftime('%Y-%m-%d %H:%M:%S')}"

        await self._send_to_all_groups(msg, "已发送Web检查通知到 ")

    # ================================================================
    # 到期提醒
    # ================================================================

    async def _check_expiry_notifications(self):
        """
        检查所有服务器的到期状态并发送通知
        每条到期日期最多触发 2 次通知: "即将到期" + "已过期"
        notify_key 格式: {ip}_{expiry_date}_{expired|approaching}
        """
        for ip, server in self.servers.items():
            if not server.expiry_date:
                continue
            days_left = self._get_days_left(server.expiry_date)
            if days_left is None:
                continue
            # 用 ip + 到期日期 + 状态区分两条独立通知
            notify_key = f"{ip}_{server.expiry_date}_{'expired' if days_left < 0 else 'approaching'}"
            if days_left <= self.expiry_notify_days:
                if notify_key not in self._notified_expiry:
                    await self._send_expiry_notification(server, days_left)
                    self._notified_expiry.add(notify_key)

    async def _send_expiry_notification(self, server: ServerInfo, days_left: int):
        """构建并发送到期通知"""
        if days_left < 0:
            msg = f"⚠️ 服务器已到期\n\n"
            msg += f"服务器: {server.name}\n"
            msg += f"IP: {server.ip}\n"
            msg += f"到期日期: {server.expiry_date}\n"
            msg += f"已过期 {-days_left} 天"
        else:
            msg = f"⏰ 服务器即将到期提醒\n\n"
            msg += f"服务器: {server.name}\n"
            msg += f"IP: {server.ip}\n"
            msg += f"到期日期: {server.expiry_date}\n"
            msg += f"剩余天数: {days_left} 天"

        await self._send_to_all_groups(msg, "已发送到期通知到 ")

    # ================================================================
    # HTML 渲染与日报
    # ================================================================

    async def _render_status_image(self) -> Optional[str]:
        """
        将服务器和 Web 检查状态渲染为 HTML 图片
        返回图片 URL (Str) 或 None (渲染失败)
        模板: templates/status.html (Jinja2)
        """
        if not self.servers and not self.web_checks:
            return None
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "status.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                html_template = f.read()
        except FileNotFoundError:
            logger.error("模板文件未找到")
            return None

        # ---- 组装服务器数据 ----
        servers_data = []
        online_count = 0
        offline_count = 0
        for ip, server in self.servers.items():
            days_left = None
            is_expired = False
            if server.expiry_date:
                days_left = self._get_days_left(server.expiry_date)
                is_expired = days_left is not None and days_left < 0
            servers_data.append({
                "name": server.name,
                "ip": server.ip,
                "is_online": server.is_online,
                "announcement": server.announcement,
                "expiry_date": server.expiry_date,
                "days_left": days_left,
                "is_expired": is_expired,
                "last_check": server.last_check_time.strftime("%H:%M:%S") if server.last_check_time else "未检测"
            })
            if server.is_online:
                online_count += 1
            else:
                offline_count += 1

        # ---- 组装 Web 检查数据 ----
        web_checks_data = []
        web_healthy_count = 0
        web_unhealthy_count = 0
        for url, check in self.web_checks.items():
            web_checks_data.append({
                "name": check.name,
                "url": check.url,
                "expected_code": check.expected_code,
                "is_healthy": check.is_healthy,
                "last_check": check.last_check_time.strftime("%H:%M:%S") if check.last_check_time else "未检测"
            })
            if check.is_healthy:
                web_healthy_count += 1
            else:
                web_unhealthy_count += 1

        # ---- 渲染 ----
        context_data = {
            "report_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_count": len(self.servers),
            "online_count": online_count,
            "offline_count": offline_count,
            "servers": servers_data,
            "web_checks": web_checks_data,
            "web_total_count": len(self.web_checks),
            "web_healthy_count": web_healthy_count,
            "web_unhealthy_count": web_unhealthy_count,
        }
        try:
            return await self.html_render(html_template, context_data)
        except Exception as e:
            logger.error(f"生成状态图片失败: {e}")
            return None

    async def _send_daily_report(self):
        """
        每日定时报告 (由 APScheduler Cron 触发)
        发送顺序: 文字摘要 (服务器 + Web 服务) -> 状态图片
        """
        if not self.target_groups:
            return

        # ---- 构建文字日报 ----
        msg = "📊 服务器状态日报\n\n"
        msg += f"报告时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        msg += f"监控服务器: {len(self.servers)} 台"

        online_count = 0
        offline_count = 0
        for ip, server in self.servers.items():
            status_emoji = "🟢" if server.is_online else "🔴"
            msg += f"\n{status_emoji} {server.name} ({ip})"
            if server.is_online:
                online_count += 1
                if server.announcement:
                    msg += f"\n   📢 {server.announcement}"
            else:
                offline_count += 1
            if server.expiry_date:
                days_left = self._get_days_left(server.expiry_date)
                if days_left is not None:
                    if days_left < 0:
                        msg += f"\n   ⚠️ 已过期 {-days_left} 天"
                    else:
                        msg += f"\n   📅 剩余 {days_left} 天"

        msg += f"\n\n━━━━━━━━━━━━━━\n"
        msg += f"服务器: 在线 {online_count} | 离线 {offline_count}"

        # ---- Web 服务日报部分 ----
        if self.web_checks:
            msg += f"\n\n🌐 Web服务检查\n\n"
            healthy_count = 0
            unhealthy_count = 0
            for url, check in self.web_checks.items():
                status_emoji = "🟢" if check.is_healthy else "🔴"
                msg += f"{status_emoji} {check.name}\n"
                msg += f"   URL: {check.url}\n"
                if check.is_healthy:
                    healthy_count += 1
                else:
                    unhealthy_count += 1
                if check.last_check_time:
                    msg += f"   最后检测: {check.last_check_time.strftime('%H:%M:%S')}\n"
                msg += "\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"Web服务: 正常 {healthy_count} | 异常 {unhealthy_count}"

        await self._send_to_all_groups(msg, "已发送日报文字到 ")

        # ---- 发送状态图片 ----
        img_url = await self._render_status_image()
        if img_url:
            img_chain = MessageChain([Image.fromURL(img_url)])
            await self._broadcast_message(img_chain, "已发送日报图片到 ")

    # ================================================================
    # 用户指令
    # ================================================================

    # ---- 服务器状态查询 ----

    @filter.command("服务器状态")
    async def check_status(self, event: AstrMessageEvent):
        """查看所有服务器和 Web 服务状态 (纯文本)"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return

        if not self.servers and not self.web_checks:
            yield event.plain_result("未配置任何服务器或Web检查")
            return

        # ---- 服务器部分 ----
        msg = "🖥️ 服务器状态查询\n\n"
        online_count = 0
        offline_count = 0

        for ip, server in self.servers.items():
            status_emoji = "🟢" if server.is_online else "🔴"
            status_text = "在线" if server.is_online else "离线"
            msg += f"{status_emoji} {server.name}\n"
            msg += f"   IP: {ip}\n"
            msg += f"   状态: {status_text}\n"

            if server.is_online:
                online_count += 1
                if server.announcement:
                    msg += f"   📢 {server.announcement}\n"
            else:
                offline_count += 1

            if server.expiry_date:
                days_left = self._get_days_left(server.expiry_date)
                if days_left is not None:
                    if days_left < 0:
                        msg += f"   ⚠️ 已过期 {-days_left} 天\n"
                    else:
                        msg += f"   📅 到期: {server.expiry_date} (剩余{days_left}天)\n"
                else:
                    msg += f"   📅 到期: {server.expiry_date}\n"

            if server.last_check_time:
                msg += f"   最后检测: {server.last_check_time.strftime('%H:%M:%S')}\n"
            msg += "\n"

        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"总计: {len(self.servers)} 台 | 在线: {online_count} | 离线: {offline_count}"

        # ---- Web 服务部分 ----
        if self.web_checks:
            msg += f"\n\n🌐 Web服务状态\n\n"
            healthy_count = 0
            unhealthy_count = 0
            for url, check in self.web_checks.items():
                status_emoji = "🟢" if check.is_healthy else "🔴"
                msg += f"{status_emoji} {check.name}\n"
                msg += f"   URL: {check.url}\n"
                msg += f"   期望状态: {check.expected_code}\n"
                if check.is_healthy:
                    healthy_count += 1
                else:
                    unhealthy_count += 1
                if check.last_check_time:
                    msg += f"   最后检测: {check.last_check_time.strftime('%H:%M:%S')}\n"
                msg += "\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"总计: {len(self.web_checks)} 个 | 正常: {healthy_count} | 异常: {unhealthy_count}"

        yield event.plain_result(msg)

    @filter.command("服务器状态图")
    async def check_status_image(self, event: AstrMessageEvent):
        """查看服务器和 Web 服务状态 (HTML 图片)"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        if not self.servers and not self.web_checks:
            yield event.plain_result("未配置任何服务器或Web检查")
            return

        img_url = await self._render_status_image()
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("生成状态图片失败")

    # ---- 手动 Ping ----

    @filter.command("ping")
    async def manual_ping(self, event: AstrMessageEvent, ip: str = ""):
        """手动 Ping 指定 IP (不限制必须在服务器列表中)"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        if not ip:
            yield event.plain_result("请提供IP地址，例如: /ping 192.168.1.1")
            return

        yield event.plain_result(f"正在检测 {ip} ...")

        # 使用 _ping_address 而非 _ping_server，支持任意 IP
        is_online = await self._ping_address(ip)

        if is_online:
            yield event.plain_result(f"🟢 {ip} 在线")
        else:
            yield event.plain_result(f"🔴 {ip} 离线或无法访问")

    # ---- 服务器管理 ----

    @filter.command("添加服务器")
    async def add_server(self, event: AstrMessageEvent, name: str, ip: str, announcement: str = "", expiry_date: str = ""):
        """动态添加服务器，自动持久化"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        if ip in self.servers:
            yield event.plain_result(f"服务器 {ip} 已存在")
            return

        self.servers[ip] = ServerInfo(
            name=name,
            ip=ip,
            announcement=announcement,
            expiry_date=expiry_date if expiry_date else None,
        )

        self._save_all_data()
        yield event.plain_result(f"✅ 已添加服务器: {name} ({ip})")

    @filter.command("删除服务器")
    async def remove_server(self, event: AstrMessageEvent, ip: str):
        """动态删除服务器，自动持久化，清理通知去重标记"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        if ip not in self.servers:
            yield event.plain_result(f"服务器 {ip} 不存在")
            return

        name = self.servers[ip].name
        del self.servers[ip]
        self._save_all_data()
        self._notified_offline.discard(ip)  # 清理离线去重标记
        yield event.plain_result(f"✅ 已删除服务器: {name} ({ip})")

    # ---- Web 检查管理 ----

    @filter.command("添加Web检查")
    async def add_web_check(self, event: AstrMessageEvent, name: str, url: str, expected_code: int = 200, must_contain: str = ""):
        """动态添加 Web 健康检查项，自动持久化"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        if url in self.web_checks:
            yield event.plain_result(f"Web检查 {url} 已存在")
            return

        self.web_checks[url] = WebCheckInfo(
            name=name,
            url=url,
            expected_code=expected_code,
            must_contain=must_contain,
        )

        self._save_all_data()
        yield event.plain_result(f"✅ 已添加Web检查: {name} ({url})")

    @filter.command("删除Web检查")
    async def remove_web_check(self, event: AstrMessageEvent, name: str):
        """按名称删除 Web 健康检查项，自动持久化，清理通知去重标记"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return

        # 按名称查找 URL (key)
        found_url = None
        for url, check in self.web_checks.items():
            if check.name == name:
                found_url = url
                break

        if not found_url:
            yield event.plain_result(f"Web检查 '{name}' 不存在")
            return

        del self.web_checks[found_url]
        self._save_all_data()
        self._notified_web_fail.discard(found_url)  # 清理 Web 异常去重标记
        yield event.plain_result(f"✅ 已删除Web检查: {name}")

    # ---- 配置 ----

    @filter.command("绑定通知群")
    async def bind_group(self, event: AstrMessageEvent):
        """
        在当前群激活通知链路
        从事件中捕获 unified_msg_origin 并缓存，用于主动消息发送
        每个配置的群组只需执行一次
        """
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        self._capture_umo(event)
        try:
            group_id = event.get_group_id()
        except (AttributeError, Exception):
            group_id = None
        if group_id:
            yield event.plain_result(f"✅ 已绑定通知群 {group_id}，主动通知将正常发送")
        else:
            yield event.plain_result("❌ 请在群聊中使用此指令")

    @filter.command("服务器帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        """显示帮助信息"""
        if not self._check_permission(event):
            yield event.plain_result("权限不足，你不在授权用户列表中")
            return
        msg = "📖 服务器监控插件帮助\n\n"
        msg += "🖥️ 服务器管理:\n"
        msg += "• /服务器状态 - 查看所有服务器和Web服务状态\n"
        msg += "• /服务器状态图 - 查看服务器状态(图片)\n"
        msg += "• /ping <IP> - 手动检测指定IP\n"
        msg += "• /添加服务器 <名称> <IP> [公告] [到期日期]\n"
        msg += "• /删除服务器 <IP>\n\n"
        msg += "🌐 Web服务检查:\n"
        msg += "• /添加Web检查 <名称> <URL> [期望状态码] [关键字]\n"
        msg += "• /删除Web检查 <名称>\n\n"
        msg += "⚙️ 其他:\n"
        msg += "• /绑定通知群 - 在当前群绑定通知链路\n"
        msg += "• /服务器帮助 - 显示此帮助"
        yield event.plain_result(msg)

    # ================================================================
    # 生命周期
    # ================================================================

    async def terminate(self):
        """插件卸载时清理: 取消监控任务、关闭调度器"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("服务器监控插件已停止")
