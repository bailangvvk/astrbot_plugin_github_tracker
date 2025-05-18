import asyncio
import aiohttp
import uuid
import logging
import json
import os
import time

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain
from astrbot.api.all import llm_tool

# 初始化 logger（插件内日志会输出到标准日志系统）
logger = logging.getLogger("GitHubTracker")
logger.setLevel(logging.DEBUG)  # 默认 DEBUG，后续可通过配置修改
handler = logging.StreamHandler()
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

@register("github_tracker", "w33d", "追踪 GitHub 仓库、指定操作（Issues/PR）、用户全部操作及生成 OpenGraph 预览图片的插件（支持自定义参数与详细日志，支持任务持久化）", "1.2", "https://github.com/Last-emo-boy/astrbot_plugin_github_tracker")
class GitHubTracker(Star):
    def __init__(self, context: Context, config: dict):
        """
        初始化插件，传入 config 参数，配置项来自 _conf_schema.json
        """
        super().__init__(context)
        self.poll_interval = config.get("poll_interval", 60)
        self.github_api_base_url = config.get("github_api_base_url", "https://api.github.com")
        self.github_token = config.get("github_token", "")
        self.notify_prefix = config.get("notify_prefix", "[GitHubTracker]")
        self.hide_errors = config.get("hide_errors", True)
        log_level_str = config.get("log_level", "DEBUG").upper()
        numeric_level = getattr(logging, log_level_str, logging.DEBUG)
        logger.setLevel(numeric_level)
        logger.debug(f"初始化插件配置：poll_interval={self.poll_interval}, github_api_base_url={self.github_api_base_url}, notify_prefix={self.notify_prefix}, log_level={log_level_str}")
        logger.debug(f"GitHub Token配置状态：{'已配置' if self.github_token else '未配置'}")
        
        # 存储API请求速率限制信息
        self.rate_limit = {
            "limit": 60 if not self.github_token else 5000,
            "remaining": 60 if not self.github_token else 5000,
            "reset": 0
        }

        # 持久化任务文件路径（与当前文件同目录）
        self.persist_file = os.path.join(os.path.dirname(__file__), "tracking_tasks.json")
        # 存储每个会话（以 unified_msg_origin 为 key）的追踪任务
        # 结构: { unified_msg_origin: { tracking_id: task_info, ... }, ... }
        self.tracking_tasks = {}

        # 加载持久化任务，并启动对应的后台轮询任务
        self.load_persistent_tasks()

    def save_tracking_tasks(self):
        """将当前追踪任务保存到文件，只保存必要的字段"""
        data = {}
        for unified_id, tasks in self.tracking_tasks.items():
            data[unified_id] = {}
            for tid, task_info in tasks.items():
                data[unified_id][tid] = {
                    "id": task_info["id"],
                    "mode": task_info["mode"],
                    "data": task_info["data"],
                    "last_event_id": task_info["last_event_id"]
                }
        try:
            with open(self.persist_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"保存持久化任务成功，共保存 {len(data)} 个会话的数据")
        except Exception as e:
            logger.exception(f"保存持久化任务失败: {str(e)}")

    def load_tracking_tasks_from_file(self):
        """从文件加载任务数据"""
        if not os.path.exists(self.persist_file):
            return {}
        try:
            with open(self.persist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.debug("加载持久化任务成功")
            return data
        except Exception as e:
            logger.exception(f"加载持久化任务失败: {str(e)}")
            return {}

    def load_persistent_tasks(self):
        """加载持久化任务，并为每个任务启动后台轮询任务"""
        persisted = self.load_tracking_tasks_from_file()
        for unified_id, tasks in persisted.items():
            if unified_id not in self.tracking_tasks:
                self.tracking_tasks[unified_id] = {}
            for tid, task_data in tasks.items():
                task_info = {
                    "id": task_data["id"],
                    "mode": task_data["mode"],
                    "data": task_data["data"],
                    "last_event_id": task_data.get("last_event_id"),
                    "task": None
                }
                if task_info["mode"] == "repo":
                    task = asyncio.create_task(self.repo_polling(unified_id, task_info))
                elif task_info["mode"] == "author":
                    task = asyncio.create_task(self.author_polling(unified_id, task_info))
                elif task_info["mode"] == "person":
                    task = asyncio.create_task(self.person_polling(unified_id, task_info))
                else:
                    continue
                task_info["task"] = task
                self.tracking_tasks[unified_id][tid] = task_info
                logger.debug(f"load_persistent_tasks: 加载任务 {tid} 模式: {task_info['mode']} for 会话 {unified_id}")

    async def send_notification(self, unified_msg_origin: str, text: str, is_error: bool = False):
        """
        发送纯文本消息，并附加通知前缀，同时记录 debug 日志
        
        Args:
            unified_msg_origin: 消息目标
            text: 消息内容
            is_error: 是否是错误消息，如果是且配置了hide_errors=True，则只记录日志不发送消息
        """
        # 如果是错误消息且配置为隐藏错误，则只记录日志
        if is_error and self.hide_errors:
            logger.debug(f"隐藏错误消息 [{unified_msg_origin}]: {text}")
            return
            
        full_text = f"{self.notify_prefix} {text}"
        logger.debug(f"send_notification to [{unified_msg_origin}]: {full_text}")
        chain = MessageChain().message(full_text)
        await self.context.send_message(unified_msg_origin, chain)

    def add_tracking_task(self, unified_msg_origin: str, task_info: dict):
        """添加新的追踪任务到会话的任务字典中，并记录日志"""
        logger.debug(f"add_tracking_task: 会话[{unified_msg_origin}], 任务ID {task_info['id']}, 模式 {task_info['mode']}")
        if unified_msg_origin not in self.tracking_tasks:
            self.tracking_tasks[unified_msg_origin] = {}
        self.tracking_tasks[unified_msg_origin][task_info["id"]] = task_info
        self.save_tracking_tasks()

    def remove_tracking_task(self, unified_msg_origin: str, tracking_id: str):
        """移除指定会话中对应的追踪任务，并记录日志"""
        logger.debug(f"remove_tracking_task: 会话[{unified_msg_origin}], 移除任务ID {tracking_id}")
        if unified_msg_origin in self.tracking_tasks:
            if tracking_id in self.tracking_tasks[unified_msg_origin]:
                del self.tracking_tasks[unified_msg_origin][tracking_id]
                if not self.tracking_tasks[unified_msg_origin]:
                    del self.tracking_tasks[unified_msg_origin]
        self.save_tracking_tasks()

    @filter.command("track_repo")
    async def track_repo(self, event: AstrMessageEvent, owner: str, repo: str):
        """
        添加一个追踪指定仓库中新 Issue 或 PR 的任务
        用法: /track_repo owner repo
        例如: /track_repo torvalds linux
        """
        unified_id = event.unified_msg_origin
        tracking_id = str(uuid.uuid4())[:8]
        task_info = {
            "id": tracking_id,
            "mode": "repo",
            "data": {"owner": owner, "repo": repo},
            "last_event_id": None,
            "task": None,
        }
        logger.debug(f"track_repo: 添加任务 {tracking_id} -> {owner}/{repo}")
        task = asyncio.create_task(self.repo_polling(unified_id, task_info))
        task_info["task"] = task
        self.add_tracking_task(unified_id, task_info)
        yield event.plain_result(f"已添加追踪仓库 {owner}/{repo} 的任务，任务 ID: {tracking_id}")

    @filter.command("track_author")
    async def track_author(self, event: AstrMessageEvent, username: str):
        """
        添加一个追踪指定用户的新 Issue 或 PR 的任务（仅筛选 IssuesEvent 与 PullRequestEvent）
        用法: /track_author username
        例如: /track_author octocat
        """
        unified_id = event.unified_msg_origin
        tracking_id = str(uuid.uuid4())[:8]
        task_info = {
            "id": tracking_id,
            "mode": "author",
            "data": {"username": username},
            "last_event_id": None,
            "task": None,
        }
        logger.debug(f"track_author: 添加任务 {tracking_id} -> 用户 {username}")
        task = asyncio.create_task(self.author_polling(unified_id, task_info))
        task_info["task"] = task
        self.add_tracking_task(unified_id, task_info)
        yield event.plain_result(f"已添加追踪用户 {username}（Issues/PR）的任务，任务 ID: {tracking_id}")

    @filter.command("track_person")
    async def track_person(self, event: AstrMessageEvent, username: str):
        """
        添加一个追踪指定用户所有公开操作的任务（包括 Push、Fork、Watch、Issues、PR 等）
        用法: /track_person username
        例如: /track_person octocat
        """
        unified_id = event.unified_msg_origin
        tracking_id = str(uuid.uuid4())[:8]
        task_info = {
            "id": tracking_id,
            "mode": "person",
            "data": {"username": username},
            "last_event_id": None,
            "task": None,
        }
        logger.debug(f"track_person: 添加任务 {tracking_id} -> 用户 {username} 全部操作")
        task = asyncio.create_task(self.person_polling(unified_id, task_info))
        task_info["task"] = task
        self.add_tracking_task(unified_id, task_info)
        yield event.plain_result(f"已添加追踪用户 {username} 所有操作的任务，任务 ID: {tracking_id}")

    @filter.command("list_track")
    async def list_track(self, event: AstrMessageEvent):
        """
        列出当前会话下所有的追踪任务
        用法: /list_track
        """
        unified_id = event.unified_msg_origin
        tasks = self.tracking_tasks.get(unified_id, {})
        if not tasks:
            logger.debug(f"list_track: 会话[{unified_id}]无任务")
            yield event.plain_result("当前没有任何追踪任务。")
            return
        lines = ["当前追踪任务列表:"]
        for tid, info in tasks.items():
            mode = info.get("mode")
            if mode == "repo":
                owner = info["data"]["owner"]
                repo = info["data"]["repo"]
                lines.append(f"- ID: {tid} | 仓库: {owner}/{repo}")
            elif mode == "author":
                username = info["data"]["username"]
                lines.append(f"- ID: {tid} | 用户（Issues/PR）: {username}")
            elif mode == "person":
                username = info["data"]["username"]
                lines.append(f"- ID: {tid} | 用户（所有操作）: {username}")
        logger.debug(f"list_track: 会话[{unified_id}]任务列表：\n" + "\n".join(lines))
        yield event.plain_result("\n".join(lines))

    @filter.command("remove_track")
    async def remove_track(self, event: AstrMessageEvent, tracking_id: str):
        """
        移除指定的追踪任务
        用法: /remove_track tracking_id
        """
        unified_id = event.unified_msg_origin
        tasks = self.tracking_tasks.get(unified_id, {})
        if tracking_id not in tasks:
            logger.debug(f"remove_track: 会话[{unified_id}]未找到任务ID {tracking_id}")
            yield event.plain_result(f"未找到任务 ID 为 {tracking_id} 的追踪任务。")
            return

        task_info = tasks[tracking_id]
        task = task_info.get("task")
        if task:
            logger.debug(f"remove_track: 正在取消任务ID {tracking_id}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug(f"remove_track: 任务ID {tracking_id} 已取消")
        self.remove_tracking_task(unified_id, tracking_id)
        yield event.plain_result(f"已移除任务 ID {tracking_id} 的追踪任务。")

    @filter.command("stop_all_track")
    async def stop_all_track(self, event: AstrMessageEvent):
        """
        停止当前会话下所有追踪任务
        用法: /stop_all_track
        """
        unified_id = event.unified_msg_origin
        tasks = self.tracking_tasks.get(unified_id, {})
        if not tasks:
            logger.debug(f"stop_all_track: 会话[{unified_id}]无任务")
            yield event.plain_result("当前没有任何追踪任务。")
            return
        for tid, info in list(tasks.items()):
            task = info.get("task")
            if task:
                logger.debug(f"stop_all_track: 正在取消任务ID {tid}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(f"stop_all_track: 任务ID {tid} 已取消")
            self.remove_tracking_task(unified_id, tid)
        yield event.plain_result("已停止所有追踪任务。")

    async def repo_polling(self, unified_msg_origin: str, task_info: dict):
        """
        后台轮询指定仓库的事件（仅关注 IssuesEvent 与 PullRequestEvent）
        """
        owner = task_info["data"]["owner"]
        repo = task_info["data"]["repo"]
        url = f"{self.github_api_base_url}/repos/{owner}/{repo}/events"
        logger.debug(f"repo_polling: 开始轮询 {owner}/{repo}, URL: {url}")

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    success, result = await self.request_github_api(session, url)
                    
                    if not success:
                        # 请求失败，发送错误通知（如果配置允许）
                        logger.error(f"repo_polling: {owner}/{repo} 请求失败: {result}")
                        await self.send_notification(unified_msg_origin, f"[{owner}/{repo}] 获取事件失败: {result}", is_error=True)
                    else:
                        # 请求成功，处理事件
                        events = result
                        logger.debug(f"repo_polling: {owner}/{repo} 返回事件数: {len(events)}")
                        new_events = []
                        for event_item in events:
                            event_type = event_item.get("type")
                            if event_type not in ["IssuesEvent", "PullRequestEvent"]:
                                continue
                            try:
                                event_id = int(event_item.get("id"))
                            except (ValueError, TypeError):
                                continue
                            if task_info["last_event_id"] is None:
                                task_info["last_event_id"] = event_id
                                logger.debug(f"repo_polling: 初始化 last_event_id 为 {event_id}")
                                break
                            if event_id > task_info["last_event_id"]:
                                new_events.append((event_id, event_item))
                        if new_events:
                            new_events.sort(key=lambda x: x[0])
                            for event_id, event_item in new_events:
                                action = event_item.get("payload", {}).get("action", "unknown")
                                title = (event_item.get("payload", {}).get("issue", {}).get("title") or
                                        event_item.get("payload", {}).get("pull_request", {}).get("title", ""))
                                msg = f"[{owner}/{repo}] 新 {event_item.get('type')}：{action} {title}"
                                logger.debug(f"repo_polling: 检测到新事件: {msg}")
                                await self.send_notification(unified_msg_origin, msg)
                            task_info["last_event_id"] = max(eid for eid, _ in new_events)
                            logger.debug(f"repo_polling: 更新 last_event_id 为 {task_info['last_event_id']}")
                            self.save_tracking_tasks()
                except Exception as e:
                    logger.exception(f"repo_polling: {owner}/{repo} 轮询异常")
                    await self.send_notification(unified_msg_origin, f"[{owner}/{repo}] 轮询时出错：{str(e)}", is_error=True)
                await asyncio.sleep(self.poll_interval)

    async def author_polling(self, unified_msg_origin: str, task_info: dict):
        """
        后台轮询指定用户的公开事件，仅筛选 IssuesEvent 与 PullRequestEvent
        """
        username = task_info["data"]["username"]
        url = f"{self.github_api_base_url}/users/{username}/events/public"
        logger.debug(f"author_polling: 开始轮询用户 {username}, URL: {url}")

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    success, result = await self.request_github_api(session, url)
                    
                    if not success:
                        # 请求失败，发送错误通知（如果配置允许）
                        logger.error(f"author_polling: 用户 {username} 请求失败: {result}")
                        await self.send_notification(unified_msg_origin, f"[{username}] 获取公开事件失败: {result}", is_error=True)
                    else:
                        # 请求成功，处理事件
                        events = result
                        logger.debug(f"author_polling: 用户 {username} 返回事件数: {len(events)}")
                        new_events = []
                        for event_item in events:
                            event_type = event_item.get("type")
                            if event_type not in ["IssuesEvent", "PullRequestEvent"]:
                                continue
                            try:
                                event_id = int(event_item.get("id"))
                            except (ValueError, TypeError):
                                continue
                            if task_info["last_event_id"] is None:
                                task_info["last_event_id"] = event_id
                                logger.debug(f"author_polling: 初始化 last_event_id 为 {event_id}")
                                break
                            if event_id > task_info["last_event_id"]:
                                new_events.append((event_id, event_item))
                        if new_events:
                            new_events.sort(key=lambda x: x[0])
                            for event_id, event_item in new_events:
                                action = event_item.get("payload", {}).get("action", "unknown")
                                title = (event_item.get("payload", {}).get("issue", {}).get("title") or
                                        event_item.get("payload", {}).get("pull_request", {}).get("title", ""))
                                msg = f"[{username}] 新 {event_item.get('type')}：{action} {title}"
                                logger.debug(f"author_polling: 检测到新事件: {msg}")
                                await self.send_notification(unified_msg_origin, msg)
                            task_info["last_event_id"] = max(eid for eid, _ in new_events)
                            logger.debug(f"author_polling: 更新 last_event_id 为 {task_info['last_event_id']}")
                            self.save_tracking_tasks()
                except Exception as e:
                    logger.exception(f"author_polling: 用户 {username} 轮询异常")
                    await self.send_notification(unified_msg_origin, f"[{username}] 轮询时出错：{str(e)}", is_error=True)
                await asyncio.sleep(self.poll_interval)

    async def person_polling(self, unified_msg_origin: str, task_info: dict):
        """
        后台轮询指定用户的所有公开事件，不做类型过滤，尽可能显示事件的关键信息
        """
        username = task_info["data"]["username"]
        url = f"{self.github_api_base_url}/users/{username}/events/public"
        logger.debug(f"person_polling: 开始轮询用户 {username} 所有操作, URL: {url}")

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(url) as resp:
                        logger.debug(f"person_polling: 用户 {username} 状态码: {resp.status}")
                        if resp.status != 200:
                            await self.send_notification(unified_msg_origin, f"[{username}] 获取公开事件失败，状态码：{resp.status}")
                        else:
                            events = await resp.json()
                            logger.debug(f"person_polling: 用户 {username} 返回事件数: {len(events)}")
                            new_events = []
                            for event_item in events:
                                try:
                                    event_id = int(event_item.get("id"))
                                except (ValueError, TypeError):
                                    continue
                                if task_info["last_event_id"] is None:
                                    task_info["last_event_id"] = event_id
                                    logger.debug(f"person_polling: 初始化 last_event_id 为 {event_id}")
                                    break
                                if event_id > task_info["last_event_id"]:
                                    new_events.append((event_id, event_item))
                            if new_events:
                                new_events.sort(key=lambda x: x[0])
                                for event_id, event_item in new_events:
                                    evt_type = event_item.get("type", "UnknownEvent")
                                    repo_name = event_item.get("repo", {}).get("name", "")
                                    payload = event_item.get("payload", {})
                                    action = payload.get("action", "")
                                    detail = action if action else str(payload)[:100]
                                    msg = f"[{username}] {evt_type} 在 {repo_name}：{detail}"
                                    logger.debug(f"person_polling: 检测到新事件: {msg}")
                                    await self.send_notification(unified_msg_origin, msg)
                                task_info["last_event_id"] = max(eid for eid, _ in new_events)
                                logger.debug(f"person_polling: 更新 last_event_id 为 {task_info['last_event_id']}")
                                self.save_tracking_tasks()
                except Exception as e:
                    logger.exception(f"person_polling: 用户 {username} 轮询异常")
                    await self.send_notification(unified_msg_origin, f"[{username}] 轮询时出错：{str(e)}")
                await asyncio.sleep(self.poll_interval)

    # ----------------------- OpenGraph 预览功能 -----------------------

    @filter.command("og_repo")
    async def og_repo(self, event: AstrMessageEvent, owner: str, repo: str):
        """
        生成指定仓库的 OpenGraph 风格预览图
        用法: /og_repo owner repo
        例如: /og_repo torvalds linux
        """
        unified_id = event.unified_msg_origin
        url = f"{self.github_api_base_url}/repos/{owner}/{repo}"
        logger.debug(f"og_repo: 获取仓库信息 {owner}/{repo}, URL: {url}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        msg = f"获取仓库信息失败，状态码：{resp.status}"
                        logger.error(f"og_repo: {msg}")
                        yield event.plain_result(msg)
                        return
                    repo_info = await resp.json()
            except Exception as e:
                logger.exception("og_repo: 异常")
                yield event.plain_result(f"获取仓库信息时出错：{str(e)}")
                return

        tmpl = """
        <div style="width:600px; padding:20px; font-family:Arial, sans-serif; background-color:#f5f5f5;">
          <h1 style="margin:0; color:#333;">{{ name }}</h1>
          <p style="color:#666;">{{ description }}</p>
          <ul style="list-style:none; padding:0;">
            <li><strong>Stars:</strong> {{ stargazers_count }}</li>
            <li><strong>Forks:</strong> {{ forks_count }}</li>
            <li><strong>Open Issues:</strong> {{ open_issues_count }}</li>
          </ul>
          <a href="{{ html_url }}" style="text-decoration:none; color:#0366d6;">查看详情</a>
        </div>
        """
        context_data = {
            "name": repo_info.get("full_name", ""),
            "description": repo_info.get("description", "暂无描述"),
            "stargazers_count": repo_info.get("stargazers_count", 0),
            "forks_count": repo_info.get("forks_count", 0),
            "open_issues_count": repo_info.get("open_issues_count", 0),
            "html_url": repo_info.get("html_url", "#")
        }
        logger.debug(f"og_repo: 渲染模板数据：{json.dumps(context_data)}")
        try:
            url_img = await self.html_render(tmpl, context_data)
            yield event.image_result(url_img)
        except Exception as e:
            logger.exception("og_repo: 渲染图片异常")
            yield event.plain_result(f"生成预览图时出错：{str(e)}")

    @filter.command("og_issue")
    async def og_issue(self, event: AstrMessageEvent, owner: str, repo: str, issue_number: int):
        """
        生成指定 Issue 的 OpenGraph 风格预览图
        用法: /og_issue owner repo issue_number
        例如: /og_issue torvalds linux 123
        """
        unified_id = event.unified_msg_origin
        url = f"{self.github_api_base_url}/repos/{owner}/{repo}/issues/{issue_number}"
        logger.debug(f"og_issue: 获取 Issue 信息 {owner}/{repo} Issue#{issue_number}, URL: {url}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        msg = f"获取 Issue 信息失败，状态码：{resp.status}"
                        logger.error(f"og_issue: {msg}")
                        yield event.plain_result(msg)
                        return
                    issue_info = await resp.json()
            except Exception as e:
                logger.exception("og_issue: 异常")
                yield event.plain_result(f"获取 Issue 信息时出错：{str(e)}")
                return

        tmpl = """
        <div style="width:600px; height:400px; background-color:#fffbe6; display:flex; flex-direction:column; justify-content:center; align-items:center; font-family:Arial, sans-serif; padding:20px; box-sizing:border-box;">
          <div style="width:100%; text-align:center;">
            <h1 style="margin:0; color:#d73a49; font-size:28px;">#{{ number }}: {{ title }}</h1>
            <p style="color:#586069; font-size:16px; margin:10px 0;">{{ body }}</p>
            <ul style="list-style:none; padding:0; margin:10px 0; font-size:16px;">
              <li><strong>状态:</strong> {{ state }}</li>
              <li><strong>评论数:</strong> {{ comments }}</li>
            </ul>
            <div>
              <a href="{{ html_url }}" style="text-decoration:none; color:#0366d6; font-size:16px;">在 GitHub 上查看</a>
            </div>
          </div>
        </div>
        """
        context_data = {
            "number": issue_info.get("number", ""),
            "title": issue_info.get("title", ""),
            "body": (issue_info.get("body", "")[:200] + "..." 
                     if issue_info.get("body") and len(issue_info.get("body")) > 200 
                     else issue_info.get("body", "")),
            "state": issue_info.get("state", ""),
            "comments": issue_info.get("comments", 0),
            "html_url": issue_info.get("html_url", "#")
        }
        logger.debug(f"og_issue: 渲染模板数据：{json.dumps(context_data)}")
        try:
            url_img = await self.html_render(tmpl, context_data)
            yield event.image_result(url_img)
        except Exception as e:
            logger.exception("og_issue: 渲染图片异常")
            yield event.plain_result(f"生成预览图时出错：{str(e)}")
    
    
    # =============================
    # LLM Function-Calling
    # =============================

    @llm_tool(name="get_repo_summary")
    async def get_repo_summary(self, event: AstrMessageEvent, owner: str, repo: str) -> MessageEventResult:
        '''获取指定 GitHub 仓库的基本信息摘要。

        Args:
            owner(string): 仓库所有者
            repo(string): 仓库名称
        '''
        url = f"{self.github_api_base_url}/repos/{owner}/{repo}"
        logger.debug(f"llm_tool get_repo_summary: 获取仓库信息 {owner}/{repo}, URL: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    yield event.plain_result(f"获取仓库信息失败，状态码：{resp.status}")
                    return
                repo_info = await resp.json()
        summary = (
            f"仓库: {repo_info.get('full_name', '未知')}\n"
            f"描述: {repo_info.get('description', '无描述')}\n"
            f"Stars: {repo_info.get('stargazers_count', 0)}\n"
            f"Forks: {repo_info.get('forks_count', 0)}"
        )
        yield event.plain_result(summary)

    @llm_tool(name="llm_track_repo")
    async def llm_track_repo(self, event: AstrMessageEvent, owner: str, repo: str) -> MessageEventResult:
        '''通过 LLM 调用，添加追踪指定仓库事件的任务。

        Args:
            owner(string): 仓库所有者
            repo(string): 仓库名称
        '''
        logger.debug(f"llm_tool llm_track_repo: 请求添加追踪仓库任务 {owner}/{repo}")
        results = []
        async for res in self.track_repo(event, owner, repo):
            results.append(res)
        # 返回最后一条消息作为确认
        yield results[-1] if results else event.plain_result("添加追踪任务失败")

    @llm_tool(name="llm_track_person")
    async def llm_track_person(self, event: AstrMessageEvent, username: str) -> MessageEventResult:
        '''通过 LLM 调用，添加追踪指定用户所有公开操作的任务。

        Args:
            username(string): 用户名
        '''
        logger.debug(f"llm_tool llm_track_person: 请求添加追踪用户全部操作任务 {username}")
        results = []
        async for res in self.track_person(event, username):
            results.append(res)
        yield results[-1] if results else event.plain_result("添加追踪任务失败")

    @llm_tool(name="get_person_activity_summary")
    async def get_person_activity_summary(self, event: AstrMessageEvent, username: str) -> MessageEventResult:
        '''通过 LLM 调用，获取指定用户最近公开活动的摘要信息。

        Args:
            username(string): 用户名
        '''
        url = f"{self.github_api_base_url}/users/{username}/events/public"
        logger.debug(f"llm_tool get_person_activity_summary: 获取用户 {username} 活动摘要, URL: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    yield event.plain_result(f"获取用户活动失败，状态码：{resp.status}")
                    return
                events = await resp.json()
        # 汇总各事件类型数量
        summary_dict = {}
        for ev in events:
            t = ev.get("type", "Unknown")
            summary_dict[t] = summary_dict.get(t, 0) + 1
        summary_lines = [f"{k}: {v}" for k, v in summary_dict.items()]
        summary = f"用户 {username} 最近活动摘要：\n" + "\n".join(summary_lines)
        yield event.plain_result(summary)

    async def get_github_api_headers(self):
        """获取GitHub API请求的头信息，包括认证令牌（如果存在）"""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-GitHubTracker"
        }
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"
        return headers
        
    async def update_rate_limit_from_response(self, response):
        """从响应头更新速率限制信息"""
        if "X-RateLimit-Limit" in response.headers:
            self.rate_limit["limit"] = int(response.headers["X-RateLimit-Limit"])
        if "X-RateLimit-Remaining" in response.headers:
            self.rate_limit["remaining"] = int(response.headers["X-RateLimit-Remaining"])
        if "X-RateLimit-Reset" in response.headers:
            self.rate_limit["reset"] = int(response.headers["X-RateLimit-Reset"])
        
        # 记录速率限制信息
        logger.debug(f"GitHub API 速率限制: 总计 {self.rate_limit['limit']}, 剩余 {self.rate_limit['remaining']}")
        
    async def request_github_api(self, session, url, method="GET"):
        """
        发送GitHub API请求，处理速率限制和认证
        
        Args:
            session: aiohttp会话对象
            url: API请求URL
            method: 请求方法，默认为GET
            
        Returns:
            成功时返回(True, 响应JSON)，失败时返回(False, 错误消息)
        """
        try:
            headers = await self.get_github_api_headers()
            
            # 检查是否接近速率限制
            if self.rate_limit["remaining"] < 5:
                now = int(time.time())
                if now < self.rate_limit["reset"]:
                    wait_time = self.rate_limit["reset"] - now + 1
                    logger.warning(f"接近API速率限制，等待 {wait_time} 秒至下次重置")
                    await asyncio.sleep(wait_time)
            
            async with session.request(method, url, headers=headers) as resp:
                # 更新速率限制
                await self.update_rate_limit_from_response(resp)
                
                # 处理响应
                if resp.status == 200:
                    return True, await resp.json()
                elif resp.status == 403 and "X-RateLimit-Remaining" in resp.headers and resp.headers["X-RateLimit-Remaining"] == "0":
                    reset_time = int(resp.headers.get("X-RateLimit-Reset", 0))
                    now = int(time.time())
                    wait_time = max(0, reset_time - now) + 1
                    error_msg = f"达到GitHub API速率限制，将在 {wait_time} 秒后重置"
                    logger.warning(error_msg)
                    return False, error_msg
                elif resp.status == 404:
                    error_msg = "请求的资源不存在（404），可能是私有仓库或用户拼写错误"
                    logger.error(f"{url}: {error_msg}")
                    return False, error_msg
                else:                    try:
                        error_data = await resp.json()
                        error_msg = f"API请求失败，状态码: {resp.status}, 错误: {error_data.get('message', '未知错误')}"
                    except:
                        error_msg = f"API请求失败，状态码: {resp.status}"
                    logger.error(f"{url}: {error_msg}")
                    return False, error_msg
        except aiohttp.ClientError as e:
            error_msg = f"API请求网络错误: {str(e)}"
            logger.exception(error_msg)
            return False, error_msg
        except asyncio.TimeoutError:
            error_msg = "API请求超时"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"API请求未知错误: {str(e)}"
            logger.exception(error_msg)
            return False, error_msg
