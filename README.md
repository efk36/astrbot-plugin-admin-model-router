# astrbot_plugin_admin_model_router

按发送者身份路由到不同的对话模型。

管理员自动切换到配置里指定的 provider；普通用户保持 AstrBot 自身配置的默认模型。指定 provider 不可用时，先按可配置次数重试探测，仍失败则自动回落到默认模型。

## 安装

**方式一：WebUI 插件市场**

在 AstrBot WebUI 的插件市场搜索「管理员模型路由」安装。

**方式二：下载 ZIP 直接用**

[astrbot_plugin_admin_model_router_v1.1.0.zip](https://github.com/efk36/astrbot-plugin-admin-model-router/releases/download/v1.1.0/astrbot_plugin_admin_model_router_v1.1.0.zip)

下载后解压，把 `astrbot_plugin_admin_model_router` 目录放进 AstrBot 的 `data/plugins/` 下，重启 AstrBot。

**方式三：从仓库安装**

```bash
cd /AstrBot/data/plugins
git clone https://github.com/efk36/astrbot-plugin-admin-model-router.git
```

`metadata.yaml` 在仓库根目录，重启 AstrBot 后生效。

## 工作方式

插件在 `on_waiting_llm_request` 阶段运行，这个时机在 AstrBot 解析 provider 之前，所以可以通过 `event.set_extra("selected_provider", provider_id)` 改写路由。

- 普通用户：插件直接返回，不设置任何值，AstrBot 照常走默认模型
- 管理员：探测可用后设置 `selected_provider`
- 探测失败：不设置，回落到默认模型，并按配置决定是否通知

目标 provider 的选择顺序（`respect_session_model` 开启时）：

1. 该会话里已经指定过的模型（WebUI 会话管理里切的，或用指令改的）
2. 配置里的 `admin_provider_id`

关闭 `respect_session_model` 则始终强制用 `admin_provider_id`。

如果 `selected_provider` 已经被其他插件或 AstrBot 自身设置过，插件会跳过，不覆盖已有选择。

探测用一个最短提示词（默认 `hi`）实际发一次请求，成功后按 `probe_cache_s` 缓存结果，避免每条消息都探测一次。

## 配置

在 AstrBot WebUI 的插件配置页面设置：

| 配置项 | 类型 | 说明 |
|---|---|---|
| `enabled` | bool | 是否启用路由 |
| `admin_provider_id` | string | 管理员使用的模型 ID |
| `extra_admin_ids` | list | 额外视为管理员的 QQ 号 |
| `respect_session_model` | bool | 是否优先用会话内已指定的模型 |
| `max_retries` | int | 探测失败时的重试次数 |
| `retry_delay_s` | float | 重试间隔（秒） |
| `probe_timeout_s` | float | 单次探测超时（秒） |
| `probe_prompt` | string | 探测用的提示词 |
| `probe_before_switch` | bool | 切换前是否先探测 |
| `probe_cache_s` | float | 探测成功缓存时长（秒） |
| `notify_on_fallback` | bool | 回落时是否通知管理员 |
| `log_switch` | bool | 是否记录路由日志 |

### 怎么填 `admin_provider_id`

填的是 **provider ID**，不是模型名。打开 `data/config/astrbot_config.json`，找到 `provider` 数组，里面每个条目形如：

```json
{
  "id": "xxx_yyy",
  "type": "openai_chat_completion",
  "model_config": { "model": "grok-4.6" }
}
```

一个 provider 里配了多个模型，在 AstrBot 里也是拆成多个条目、各有各的 `id`，填 `id` 字段而不是 `model_config.model` 的值。

填错的话日志里会出现「找不到 provider」，此时同一条日志会列出当前所有可用的 provider ID -> 模型名，`/模型路由` 命令也会输出这份列表。

## 命令

- `/模型路由` - 查看当前路由配置。管理员可见，会显示实际解析到的模型名和选择来源

## 日志示例

```
[admin_model_router] probe ok on attempt 1/2
[admin_model_router] sender=2663179395 routed to provider `grok/grok-4.6` (source=config)
```

`source=config` 表示用的是配置里的 `admin_provider_id`，`source=session` 表示用的是会话内指定的模型。

## 更新日志

### 1.1.0

- 新增 `respect_session_model`：会话内已指定模型时优先跟随，无指定才用 `admin_provider_id`
- 找不到 provider 时，日志和 `/模型路由` 会列出当前可用的 provider ID -> 模型名
- `selected_provider` 已被其他插件设置时跳过，不覆盖
- 仓库结构改为 `metadata.yaml` 在根目录，修复从仓库安装时提示「未在仓库根目录找到 metadata.yaml」的问题
- `metadata.yaml` 补充 `short_desc`、`tags`、`support_platforms`、`social_link`，便于在插件市场被检索到

## 兼容性

需要 AstrBot >= 4.0.0。在 aiocqhttp (OneBot v11) 平台上测试通过。