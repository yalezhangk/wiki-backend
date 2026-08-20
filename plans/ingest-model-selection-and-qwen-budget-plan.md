# Ingest 模型选择与 Qwen 上下文预算计划

## 目标

为 Ingest 提供独立于全局 `WIKI_BACKEND_LLM_*` 的服务端模型选择，确保本地
`ollama_chat/qwen3.6:35b` 使用已确认的 65536 token 上下文限制。云端 DeepSeek
模型在其窗口能力未被确认前，不强行配置或假定本地上下文限制。

本计划只涉及模型选择、Qwen prompt 硬检查、任务模型记录和相应文档/测试；不改变
Wiki 检索策略，也不实现上下文缩减或长文档分块。

## 当前问题

`IngestService` 当前通过 `call_llm_main()` 调用全局主模型：

- `WIKI_BACKEND_LLM_PROVIDER`
- `WIKI_BACKEND_LLM_MAIN_MODEL`
- `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS`

其中最后一项只控制单次输出 token；没有独立的 Ingest provider/model 选择，也没有
针对本地模型的输入、输出和安全余量包络检查。因此，切换全局模型或使用 Qwen 时，
Ingest 不能可靠地按模型能力约束最终 prompt。

## 已确认的设计决定

1. `.env` 只选择 Ingest 模型，不为每个模型增加四个 token 配置项。
2. 模型名称、provider、推理方式和已知能力由服务端白名单控制；API 客户端不得传入
   provider、model、API 地址或 token 预算。
3. 已确认的 `ollama_chat/qwen3.6:35b` 使用固定包络：

   ```text
   context_window = 65536
   max_input = 49152
   max_output = 8192
   safety_margin = 8192
   max_input + max_output + safety_margin = context_window
   ```

4. DeepSeek V4 Pro 与 Flash 的实际窗口能力尚未确认。它们使用
   `unbounded_provider_managed` 模式：不做本地上下文窗口预检，不虚构窗口或安全余量；
   仍使用配置的 `max_tokens` 限制输出，并由云端 API 执行最终窗口处理。
5. `ingest_jobs` 只记录实际执行的规范化模型标识，例如
   `deepseek/deepseek-v4-pro` 或 `ollama_chat/qwen3.6:35b`；不持久化 token 预算。
6. 本次不实现 Wiki 上下文缩减、候选检索调整、长文档分块或摘要归并。Qwen 的最终
   prompt 超窗时必须失败，不调用模型、不写 Wiki。

## 拟新增的 `.env.example` 配置

```env
# Ingest 专用模型；只允许服务端白名单中的 provider/model 组合。
WIKI_BACKEND_INGEST_PROVIDER=deepseek
WIKI_BACKEND_INGEST_MODEL=deepseek-v4-pro

# Ingest 单次 JSON 生成的最大输出 token。
WIKI_BACKEND_INGEST_LLM_MAX_TOKENS=8192

# 仅本地 Qwen 结构化 JSON 生成建议关闭思考；DeepSeek 留空以使用服务端默认策略。
WIKI_BACKEND_INGEST_REASONING_EFFORT=
```

切换至本地 Qwen：

```env
WIKI_BACKEND_INGEST_PROVIDER=ollama_chat
WIKI_BACKEND_INGEST_MODEL=qwen3.6:35b
WIKI_BACKEND_INGEST_LLM_MAX_TOKENS=8192
WIKI_BACKEND_INGEST_REASONING_EFFORT=none
```

新增模型时，只需选择对应的 provider/model。若未来确认某模型需要本地 prompt 硬检查，
在服务端能力白名单增加一条能力记录，而非在 `.env` 添加四项预算变量。

## 实施步骤

1. 在 `app/config.py` 增加 Ingest 专用 provider、model 和 reasoning 配置；保留
   `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 作为 Ingest 输出上限。
2. 在 LLM 配置层新增服务端受控的 Ingest 模型解析与能力注册表：
   - 白名单至少包含 `deepseek/deepseek-v4-pro`、`deepseek/deepseek-v4-flash` 和
     `ollama_chat/qwen3.6:35b`。
   - Qwen 返回固定的 65536 包络。
   - DeepSeek 返回无本地窗口限制的能力标记。
   - 不复用浏览器 Chat 的 `model_profile_id` 作为 Ingest 输入。
3. `IngestService` 使用解析后的 Ingest 模型调用 `call_llm_profile()`，不再调用
   `call_llm_main()`；实际 LiteLLM `max_tokens` 必须等于 Ingest 输出配置。
4. 在每次 LLM 调用前，对最终 `render_prompt()` 结果进行 token 估算：
   - Qwen 必须满足 `input <= 49152` 且
     `input + configured_output + 8192 <= 65536`。
   - 不满足时抛出稳定的 `ingest_source_context_too_large` 错误。
   - DeepSeek 在能力未知时跳过该预检，并在结构化日志标记
     `budget_mode=unbounded_provider_managed`。
5. 为 `ingest_jobs` 增加 `ingest_model` 字段，并在创建任务时写入实际选择的规范化
   模型标识。worker 必须按该任务记录的模型执行，避免排队后配置变化导致记录与实际
   调用不一致。
6. 同步更新 `.env.example`、README、Pydantic schema/API 文档（如任务详情公开该字段）
   和迁移/初始化逻辑。

## 验证标准

- Qwen 配置下，服务端调用 `ollama_chat/qwen3.6:35b` 且传入 `max_tokens=8192`。
- Qwen 的最终 prompt 在包络内时允许调用；超窗时任务失败且不发生 LLM/Wiki 写入。
- DeepSeek Pro 与 Flash 可在未知窗口模式下调用，且不被 Qwen 的 65536 限制误拦截。
- 非白名单 provider/model 在服务启动或任务创建时明确失败。
- `ingest_jobs.ingest_model` 与实际调用的规范化模型标识一致。
- 相关单元测试、配置加载测试和 API/存储迁移测试通过。

## 明确排除项

- Wiki 上下文缩减与确定性候选检索。
- 长文档分块、提取、摘要和归并。
- 云端 DeepSeek 窗口能力的猜测或硬编码；待实际供应商能力确认后另行补充。
- 让浏览器或 Ingest API 请求决定模型、provider、API 地址或 token 预算。
