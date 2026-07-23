# wiki-backend：Chats 行内引用格式交接

## 目标

Quartz Chats 已调整为：

- 正文不显示末尾的 `## Sources`、`## Source` 或 `## 引用来源` 清单；
- 正文中的 `[1]`、`[2]` 等标记会定位到右侧既有的“引用来源”条目；
- 右侧“引用来源”继续展示完整的 `sources` 与 `relevant_pages`，标题和现有 API 路由不变。

## 当前接口契约

现有 `GET /api/chats/{chat_id}/messages` 与发送消息响应中的 assistant message 已包含：

```json
{
  "content": "回答正文",
  "sources": ["sources/example.md"],
  "relevant_pages": ["entities/example.md"]
}
```

第一阶段**不需要**新增路由、请求字段、响应字段、数据库字段或迁移。

## 后端需要调整

调整生成回答的提示词和生成结果校验：

1. `content` 只输出回答 Markdown，不再附加 `## Sources`、`## Source` 或 `## 引用来源` 标题和资料列表。
2. 对有明确依据的关键结论，在相应句末输出 `[n]`。
3. `n` 从 1 开始，严格对应本次响应 `sources` 数组去重后的顺序。
4. `sources` 必须使用稳定的 Wiki 页面路径；推荐 `sources/xxx.md`、`entities/xxx.md` 这种仓库相对路径。Quartz 会兼容 `.md` 后缀并跳转到无扩展名页面。
5. `relevant_pages` 是拓展阅读，不应使用 `[n]` 行内标记。
6. 没有检索依据的陈述不得伪造引用标记。

示例：

```json
{
  "content": "施耐德电气在该资料中被描述为相关产品与方案的厂商主体。[1]\\n\\nPIX 系列属于其空气绝缘开关柜产品线。[2]",
  "sources": [
    "entities/施耐德电气.md",
    "entities/PIX.md"
  ],
  "relevant_pages": [
    "concepts/气体绝缘开关柜.md"
  ]
}
```

## 推荐校验

- 删除模型输出末尾可能遗留的 `Sources` 区块；
- 校验所有 `[n]` 均满足 `1 <= n <= 去重后的 sources 数量`；
- 未被任何标记使用的 `sources` 仍可保留在右侧“引用来源”，作为完整证据清单；
- 对同一来源重复检索时，后端应在写响应前保持 `sources` 的稳定去重顺序。

## 后续可选增强

若需要把每个断言与来源精确绑定，并避免模型输出错误编号，再扩展响应为可选字段：

```json
{
  "citations": [
    { "marker": 1, "source": "entities/施耐德电气.md" },
    { "marker": 2, "source": "entities/PIX.md" }
  ]
}
```

此字段不是本次 Quartz UI 上线的前置条件。

## 验收

1. 新发起的回答正文不再出现 `Sources` 标题或重复来源列表。
2. 点击正文 `[1]` 会定位到右侧第一条 `sources`。
3. 右侧“引用来源”仍显示全部 `sources` 与 `relevant_pages`。
4. 现有 Chats、Synthesis、Ingest 路由和历史消息读取行为保持兼容。
