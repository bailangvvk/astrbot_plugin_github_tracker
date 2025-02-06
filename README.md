# GitHubTracker

GitHubTracker 插件是一个用于 AstrBot 的插件，能够实现以下功能：

- **追踪 GitHub 仓库**：实时轮询指定仓库的事件（仅关注 Issue 和 Pull Request），在有新事件时通知用户。
- **追踪用户操作**：可选择只关注用户与 Issue/PR 相关的操作，或追踪用户所有公开操作（包括 Push、Fork、Watch 等）。
- **OpenGraph 预览**：通过 HTML 模板生成 GitHub 仓库和 Issue 的预览图，支持在聊天中直接发送图片预览。
- **任务管理**：支持同一会话下同时运行多个追踪任务，提供添加、查看、删除单个任务或全部停止任务的命令。
- **详细日志**：插件内部集成了详细的 DEBUG 日志记录，便于开发调试和问题排查。
- **自定义参数**：支持通过配置文件自定义轮询间隔、GitHub API 基础 URL、通知前缀及日志输出等级等参数。

## 文件结构

插件目录下包含以下文件：

```
.
├── _conf_schema.json   # 插件配置文件（JSON 格式）
└── main.py             # 插件代码实现
```

## 配置说明

配置文件 **_conf_schema.json** 用于定义插件可自定义的参数，示例如下：

```json
{
  "poll_interval": {
    "description": "轮询间隔（单位：秒）",
    "type": "int",
    "hint": "每次轮询 GitHub API 的间隔时间，默认值为 60 秒",
    "default": 60
  },
  "github_api_base_url": {
    "description": "GitHub API 基础 URL",
    "type": "string",
    "hint": "GitHub API 的基础 URL，默认值为 https://api.github.com",
    "default": "https://api.github.com"
  },
  "notify_prefix": {
    "description": "通知前缀",
    "type": "string",
    "hint": "通知消息前附加的前缀，用于标识消息来源",
    "default": "[GitHubTracker]"
  },
  "log_level": {
    "description": "日志输出等级",
    "type": "string",
    "hint": "日志等级，可选值 DEBUG/INFO/WARNING/ERROR，默认 DEBUG",
    "default": "DEBUG"
  }
}
```

当插件加载时，AstrBot 会自动解析该配置文件，并将配置传入插件构造函数。

## 使用方法

### 追踪任务命令

插件支持以下命令来添加和管理追踪任务。所有命令均通过 AstrBot 消息指令触发，命令参数之间用空格分隔。

- **/track_repo owner repo**  
  追踪指定仓库中新 Issue/PR 的事件。  
  **示例**:  
  ```
  /track_repo torvalds linux
  ```
  添加后，插件将定时调用 GitHub API，检测仓库事件变化，并在检测到新事件时通知用户。

- **/track_author username**  
  追踪指定用户（仅关注 Issue/PR 相关操作）的公开事件。  
  **示例**:  
  ```
  /track_author octocat
  ```

- **/track_person username**  
  追踪指定用户所有公开操作（包括 Push、Fork、Watch、Issue、PR 等），并尝试输出关键信息。  
  **示例**:  
  ```
  /track_person octocat
  ```

- **/list_track**  
  查看当前会话下所有追踪任务及对应的任务 ID。  
  **示例**:  
  ```
  /list_track
  ```

- **/remove_track tracking_id**  
  移除指定的追踪任务。  
  **示例**:  
  ```
  /remove_track ab12cd34
  ```

- **/stop_all_track**  
  停止当前会话下所有追踪任务。  
  **示例**:  
  ```
  /stop_all_track
  ```

### OpenGraph 预览命令

插件支持生成 OpenGraph 风格的预览图，通过 HTML 模板渲染生成图片，并在聊天中展示。

- **/og_repo owner repo**  
  生成指定仓库的预览图，显示仓库名称、描述、星标、Fork 数及 Issue 数等信息。  
  **示例**:  
  ```
  /og_repo torvalds linux
  ```

- **/og_issue owner repo issue_number**  
  生成指定 Issue 的预览图，显示 Issue 编号、标题、部分正文、状态和评论数。  
  **示例**:  
  ```
  /og_issue torvalds linux 123
  ```

预览功能依赖插件内置的 `html_render` 方法，将 HTML+Jinja2 模板渲染为图片 URL，然后发送图片消息。

## 示例

以下是一些使用示例：

1. **添加仓库追踪任务：**
   ```
   /track_repo torvalds linux
   ```
   插件将开始轮询 [torvalds/linux](https://github.com/torvalds/linux) 仓库的事件，并在检测到新 Issue/PR 时通知用户。

2. **添加用户全部操作追踪任务：**
   ```
   /track_person octocat
   ```
   插件将监控用户 `octocat` 所有公开操作，并发送通知。

3. **生成仓库预览图：**
   ```
   /og_repo torvalds linux
   ```
   插件获取仓库信息并生成预览图返回。

4. **生成 Issue 预览图：**
   ```
   /og_issue torvalds linux 123
   ```
   插件获取 Issue 信息，并生成预览图返回。

---

如有其他疑问或需求，请在 [issue](https://github.com/Last-emo-boy/astrbot_plugin_github_tracker) 反馈。  
