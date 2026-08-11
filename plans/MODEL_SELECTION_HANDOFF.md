# 模型选择功能交接

> 状态（2026-08-10）：模型选择已在 `wiki-backend` 与 Quartz 实现。当前后端提供
> `GET /api/model-profiles` 和 `GET /api/model-profiles/overview`，聊天请求必须提交
> `model_profile_id`，助手消息持久化模型 ID 与显示名称快照。本文保留决策和验证背景，
> 现行契约以 `README.md`、`app/model_profiles.py`、`app/api/model_profiles.py` 与测试为准。

## 目标

让知识问答用户在发送消息前选择**回答模型/模式**，而不是修改服务端全局 `.env`。首批可选项为：

| 用户看到的选项 | `model_profile_id` | 执行位置 | 推理策略 | 适用说明 |
| --- | --- | --- | --- | --- |
| DeepSeek V4 Pro | `deepseek-v4-pro` | 云端 | 由云端服务管理 | 复杂、质量优先的问答 |
| DeepSeek V4 Flash | `deepseek-v4-flash` | 云端 | 由云端服务管理 | 常规、响应速度优先的问答 |
| Qwen3.6 35B · 直接回答 | `local-qwen3.6-35b-direct` | 本地 Ollama | `think=false` | 本地部署、低延迟优先 |
| Qwen3.6 35B · 深度思考 | `local-qwen3.6-35b-thinking` | 本地 Ollama | `think=true` | 本地部署、复杂推理优先 |

这里使用“回答模式”而非单独的“模型 + 思考开关”两个控件。四个预设可避免用户选择无效组合，例如误把 DeepSeek 模型标成 Qwen 的 thinking/non-thinking 模式。

## Qwen3.6 35B：为什么会慢

目标模型 `qwen3.6:35b` 需在部署环境中验证 thinking 支持情况。Ollama 对支持的模型默认开启 thinking；开启时会先生成独立的推理内容，再给出最终回答。`think=false` 可以跳过这段推理，因此通常能明显缩短生成时间，但不会减少 35B 模型加载、长上下文提示词处理或 GPU/CPU offload 的耗时。[Ollama Thinking 文档](https://docs.ollama.com/capabilities/thinking) 是本结论的依据。

当前 `wiki-backend/app/llm_config.py` 会把本地 Qwen 档案的 `reasoning_effort` 传给 LiteLLM：direct 使用 `"none"`，thinking 使用 `"low"`，由 Ollama Chat 适配器分别映射为 `think=false`、`think=true`。Chats API 仍不是流式响应，所以用户会把生成 reasoning 与最终答案完整感知为一次等待。

### 已完成的 LiteLLM / Ollama 兼容性验证

既有验证使用 `litellm==1.82.6`、此前的本地 Qwen 模型和 Ollama `http://127.0.0.1:11434`；切换为 `ollama_chat/qwen3.6:35b` 后须重新完成下述实机验证。

- 对此版本 LiteLLM，profile 不应直接向 `completion(...)` 传 `think`。其 Ollama Chat 适配器将标准参数 `reasoning_effort` 映射到发送给 `POST /api/chat` 的顶层 `think`：`"none"` 映射为 `false`，`"low"`、`"medium"`、`"high"` 映射为 `true`。
- 当前两个本地 profile 的后端内部配置固定为：`local-qwen3.6-35b-direct -> reasoning_effort="none"`，`local-qwen3.6-35b-thinking -> reasoning_effort="low"`。只有本地 Qwen profile 传此参数；DeepSeek profile 不传。
- 实机请求已证明两种模式都能产生最终答案：direct 返回 `reasoning_chars=0`；thinking 返回 `reasoning_chars=1602` 且同时返回最终 `content`，两者的 `finish_reason` 都是 `"stop"`。这证明 LiteLLM、当前 Ollama 和模型 tag 的组合可用。
- 先前以 `max_tokens=64` 测试 thinking 时只有 reasoning、没有最终 `content`。当前服务端固定 Direct profile 为 `1024`、Thinking profile 为 `2048`，避免两种模式共用过低的输出上限；上线后仍要结合截断率、质量、延迟和成本实测调整。
- 单次总耗时不能决定默认 profile：已观测到 direct 从约 `4.10s` 到 `56.44s` 的波动，thinking 一次为 `38.63s`。模型冷启动/驻留、GPU 排队及系统负载都会影响结果。后续性能试验必须先预热，再交替各运行至少 3 次，以中位数决策，并记录首 token、总耗时、prompt/eval tokens、tokens/s 和 GPU/CPU offload。

实现后必须保留两类验证：

1. 协议级自动化测试：用临时假 Ollama HTTP 服务承接 LiteLLM 请求，断言 direct 的请求体为 `POST /api/chat` 且 `think: false`，thinking 为 `think: true`。假响应可同时包含 `message.thinking` 与 `message.content`，测试必须断言后端仅返回和持久化最终 `content`，不记录或展示 reasoning 原文。
2. DGX 实机冒烟：用项目 `.venv/bin/python` 分别发送 `reasoning_effort="none"` 与 `"low"` 的短请求，断言两种请求均为 `finish_reason="stop"`、最终 `content` 非空；不得仅以 HTTP 成功或存在 reasoning 判断成功。

不要在 Quartz 浏览器端直连 `11434`。本地 profile 使用 LiteLLM provider `ollama_chat` 和根 `api_base`（例如 `http://127.0.0.1:11434`，不追加 `/v1`）。

UI 不展示、保存或回传模型的原始推理内容。界面中的“深度思考”只是用户可理解的执行策略标签；运行中可以展示“正在深度推理并整理答案…”，最终只显示答案与引用。

## 模型角色的界定

现有后端有两个内部角色：

- `FAST_MODEL`：Wiki 页面选择等检索辅助工作；保持为服务端控制的内部模型，不给普通问答用户选择。
- `MAIN_MODEL`：问答答案生成和 ingest 等主任务。Chats 的模型选择只替代**本轮答案生成**使用的主模型档案。

首个版本不应让用户通过 Chats 改变 ingest、synthesis、质量检查或发布任务的模型；这些仍使用服务端受控的默认主模型。这样“本轮问答选择”与“系统运行配置”边界清晰。

为避免模型配置含义混乱，当前实现将 Chat 回答模型与内部 FAST/MAIN 配置分开：Chat 使用后端拥有的**受控模型档案（profile）白名单**，内部任务仍使用 `WIKI_BACKEND_LLM_PROVIDER`、`WIKI_BACKEND_LLM_FAST_MODEL`、`WIKI_BACKEND_LLM_MAIN_MODEL`。每个 Chat 档案在服务器端映射 provider、模型名、API base、凭据引用、可用性、thinking 策略及固定 token/temperature；浏览器只能获得安全的 ID、显示名称、执行位置、策略和可用状态。

## Quartz UI 方案

模型选择放在知识问答输入卡片中，而不放在“系统设置”作为普通用户的操作入口。

```text
继续追问                                      回答模式 [DeepSeek V4 Flash · 云端 · 快速 v]
[上传]  继续追问，或输入新的相关问题                                              [发送]
         本轮将使用 DeepSeek V4 Flash；切换仅影响后续回答。
```

位置与交互规则：

1. 在 `.local-plugins/chats/src/components/ChatPage.tsx` 的 `.chat-input-label` 同一行右侧放置选择器；窄屏时自动换至输入框下方。
2. 下拉按“云端模型”和“本地模型”分组，直接呈现上表四个预设；不可用项禁用并说明“当前不可用”。
3. 选择保存到 `sessionStorage`，作为该浏览器后续新消息的默认值，但每次发送都允许修改。
4. 用户在已有会话中切换时，提示“切换仅影响本轮及后续回答，历史回答保持原模型”。
5. 每条助手消息的元信息中显示实际档案徽标，例如 `Qwen3.6 35B · 直接回答`，从而使混合模型的历史可追溯。
6. “系统设置”页改为只读的“可用回答模型”概览：名称、云端/本地、默认项、可用状态和简短用途。只有后续引入管理员鉴权与后端配置 API 后，才在此页增加“默认问答模型”管理。

## wiki-backend 实现交接

### 当前 API 契约

```text
GET  /api/model-profiles
GET  /api/model-profiles/overview
POST /api/chats/{chat_id}/messages
     { "content": "...", "model_profile_id": "local-qwen3.6-35b-direct" }
```

`GET /api/model-profiles` 只返回可公开数据，例如：

```json
{
  "id": "local-qwen3.6-35b-direct",
  "label": "Qwen3.6 35B · 直接回答",
  "location": "local",
  "reasoning_mode": "direct",
  "available": true,
  "is_default": false
}
```

服务端对 `model_profile_id` 做 Pydantic 枚举/白名单校验；未知、禁用或不健康的 profile 返回 `422` 或 `503`，绝不能接受浏览器传入的 provider、模型名、`api_base`、API key 或任意 LiteLLM 参数。

必须在创建本轮用户消息、调用 LiteLLM 之前完成 profile 解析与可用性判断。否则无效 profile 会在数据库留下没有回答的用户消息。错误响应应让 UI 可区分：未知/格式错误为 `422`，已启用但当前不可用为 `503`；不得静默回退到另一模型。

profile 的“已启用”和“当前可用”是两个状态：`GET /api/model-profiles` 不返回未启用的 profile；已启用但健康检查暂不可用的 profile 保留在列表中并返回 `available: false`，供 UI 禁用。健康状态应有缓存或后台刷新，不能在每次加载页面时同步探测模型。

`ChatMessageCreateRequest`、`ChatTurnService`、`QueryService` 与 `app/llm_config.py` 已把本轮 profile 一路显式传递。`QueryService` 的检索辅助仍调用固定 FAST 配置，最终回答使用已解析的 profile。Profile ID 和实际显示标签快照写入 assistant message，并在 `ChatMessageResponse` 中返回；读取历史时不会用当前 profile 配置重新推导标签。

当前 MySQL 启动初始化/升级逻辑会为已有 `chat_messages` 补充可空的 profile ID 与标签字段；旧消息读取时保持为空。Fake storage、API 测试和 MySQL 集成测试覆盖相同兼容边界。

### 必测项

- profile 列表不含凭据、内网地址或未启用模型。
- 无效 profile 不能发起 LiteLLM 调用；禁用/不可用 profile 的错误可供 UI 呈现。
- direct 和 thinking profile 分别通过 LiteLLM 的 `reasoning_effort="none"`、`"low"` 发出 Ollama `think: false`、`true`；thinking 响应必须同时有最终答案，不能只产生 reasoning。
- DeepSeek 两个 profile 使用各自服务端凭据，不回落到本地 Ollama。
- 历史消息返回的 profile 徽标正确；旧消息字段为空时 UI 平稳降级为“历史模型未知”。
- 现有 `POST /api/query` 保持无状态，不隐式新增 profile 持久化或聊天会话。

## Quartz 实现交接

相关位置：

- `.local-plugins/chats/src/components/ChatPage.tsx`：输入卡片结构与安全的 server-rendered 默认状态。
- `.local-plugins/chats/src/components/scripts/chat.inline.ts`：加载 profile、选择状态、发送体、历史徽标与 SPA 生命周期清理。
- `.local-plugins/chats/src/api/chatApi.ts`：`GET /api/model-profiles` 和带 `model_profile_id` 的消息请求。
- `.local-plugins/chats/src/components/styles/chat.scss`：桌面/窄屏布局、下拉项、可用状态与消息徽标。
- `.local-plugins/chats/src/types.ts`：profile 和消息返回字段类型。

Quartz 只通过同源 `/api` 调用后端，不能把 Ollama `11434` 或后端 `8081` 暴露给浏览器。源码变更后必须构建 `chats/dist`，再构建 Quartz `public/`；不要手改生成物。

## 已完成的实施顺序

1. 在 `wiki-backend` 先定义 profile 配置模型、健康/可用性规则、API schema、消息持久化与测试。
2. 用两种 Qwen 模式做端到端试验，记录首 token、总耗时、tokens/s、prompt tokens、GPU/CPU offload；基于实测再决定 UI 默认项，而不是仅凭“35B 应该慢/快”。
3. 在 `quartz` 接入 profile 列表与输入区选择器，处理未加载、不可用、请求失败和历史兼容状态。
4. 分别构建插件和 Quartz，再用同源 `/api` 验证真实站点；最后在 DGX 上验证，不以 Windows 构建成功代替部署验证。

## 非目标

- 不让用户编辑 `.env`、模型服务地址、API key、Prompt 或 FAST 模型。
- 不在浏览器中显示完整 reasoning trace。
- 不改动 `llm-wiki-agent` 源码。
- 不新增 Ollama 或后端公网/局域网直通端口。
