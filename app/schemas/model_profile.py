from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ModelProfileId = Literal[
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "local-qwen3.6-35b-direct",
    "local-qwen3.6-35b-thinking",
]


class ModelProfileResponse(BaseModel):
    """可安全提供给浏览器的回答模型档案。"""

    id: ModelProfileId = Field(description="服务端受控的回答模型档案 ID。")
    label: str = Field(description="回答模型的显示名称。")
    location: Literal["cloud", "local"] = Field(description="模型的执行位置。")
    reasoning_mode: Literal["provider_managed", "direct", "thinking"] = Field(
        description="面向用户的推理策略说明，不包含原始推理内容。"
    )
    available: bool = Field(description="该已启用档案当前是否可用于发送消息。")
    is_default: bool = Field(description="该档案是否为新消息的服务端默认选项。")


class InternalModelResponse(BaseModel):
    """服务端内部任务当前使用的模型配置概览。"""

    provider: str = Field(description="当前内部任务使用的 LiteLLM provider。")
    model: str = Field(description="当前内部任务使用的模型名。")


class ModelOverviewResponse(BaseModel):
    """系统设置页所需的只读模型概览。"""

    chat_models: list[ModelProfileResponse] = Field(
        description="知识问答 Chat 当前可选择的已启用模型档案。"
    )
    fast_model: InternalModelResponse = Field(
        description="快速问答内部任务当前使用的模型。"
    )
    main_model: InternalModelResponse = Field(
        description="深度分析内部任务当前使用的模型。"
    )
