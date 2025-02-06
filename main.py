import asyncio
import aiohttp
import uuid
import logging
import json

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain

# 初始化 logger（插件内日志会输出到标准日志系统）
logger = logging.getLogger("GitHubTracker")
logger.setLevel(logging.DEBUG)  # 默认 DEBUG，后续可通过配置修改
handler = logging.StreamHandler()
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

@register("github_tracker", "Your Name", "追踪 GitHub 仓库、指定操作（Issues/PR）、用户全部操作及生成 OpenGraph 预览图片的插件（支持自定义参数与详细日志）", "1.0.0", "https://github.com/your_repo")
class GitHubTracker(Star):
    def __init__(self, context: Context, config: dict):
        """
        初始化插件，传入 config 参数，配置项来自 _conf_schema.json
        """
        super().__init__(context)
        self.poll_interval = config.get("poll_interval", 60)
        self.github_api_base_url = config.get("github_api_base_url", "https://api.github.com")
        self.notify_prefix = config.get("notify_prefix", "[GitHubTracker]")
        log_level_str = config.get("log_level", "DEBUG").upper()
        numeric_level = getattr(logging, log_level_str, logging.DEBUG)
        logger.setLevel(numeric_level)
        logger.debug(f"初始化插件配置：poll_interval={self.poll_interval}, github_api_base_url={self.github_api_base_url}, notify_prefix={self.notify_prefix}, log_level={log_level_str}")
        
        # 存储每个会话（以 unified_msg_origin 为 key）的追踪任务
        # 结构: { unified_msg_origin: { tracking_id: task_info, ... }, ... }
        self.tracking_tasks = {}

    async def send_notification(self, unified_msg_origin: str, text: str):
        """发送纯文本消息，并附加通知前缀，同时记录 debug 日志"""
        full_text = f"{self.notify_prefix} {text}"
        logger.debug(f"send_notification to [{unified_msg_origin}]: {full_text}")
        # 构造 MessageChain 对象
        chain = MessageChain().message(full_text)
        await self.context.send_message(unified_msg_origin, chain)

    def add_tracking_task(self, unified_msg_origin: str, task_info: dict):
        """添加新的追踪任务到会话的任务字典中，并记录日志"""
        logger.debug(f"add_tracking_task: 会话[{unified_msg_origin}], 任务ID {task_info['id']}, 模式 {task_info['mode']}")
        if unified_msg_origin not in self.tracking_tasks:
            self.tracking_tasks[unified_msg_origin] = {}
        self.tracking_tasks[unified_msg_origin][task_info["id"]] = task_info

    def remove_tracking_task(self, unified_msg_origin: str, tracking_id: str):
        """移除指定会话中对应的追踪任务，并记录日志"""
        logger.debug(f"remove_tracking_task: 会话[{unified_msg_origin}], 移除任务ID {tracking_id}")
        if unified_msg_origin in self.tracking_tasks:
            if tracking_id in self.tracking_tasks[unified_msg_origin]:
                del self.tracking_tasks[unified_msg_origin][tracking_id]
                if not self.tracking_tasks[unified_msg_origin]:
                    del self.tracking_tasks[unified_msg_origin]

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
                    async with session.get(url) as resp:
                        logger.debug(f"repo_polling: {owner}/{repo} 状态码: {resp.status}")
                        if resp.status != 200:
                            await self.send_notification(unified_msg_origin, f"[{owner}/{repo}] 获取事件失败，状态码：{resp.status}")
                        else:
                            events = await resp.json()
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
                except Exception as e:
                    logger.exception(f"repo_polling: {owner}/{repo} 轮询异常")
                    await self.send_notification(unified_msg_origin, f"[{owner}/{repo}] 轮询时出错：{str(e)}")
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
                    async with session.get(url) as resp:
                        logger.debug(f"author_polling: 用户 {username} 状态码: {resp.status}")
                        if resp.status != 200:
                            await self.send_notification(unified_msg_origin, f"[{username}] 获取公开事件失败，状态码：{resp.status}")
                        else:
                            events = await resp.json()
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
                except Exception as e:
                    logger.exception(f"author_polling: 用户 {username} 轮询异常")
                    await self.send_notification(unified_msg_origin, f"[{username}] 轮询时出错：{str(e)}")
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

        # 构造 HTML 模板，展示仓库名称、描述、星标、fork 数等信息
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

        # 修改后的 HTML 模板，使用固定尺寸及 Flex 布局使内容居中
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
            "body": issue_info.get("body", "")[:200] + "..." if issue_info.get("body") and len(issue_info.get("body")) > 200 else issue_info.get("body", ""),
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

