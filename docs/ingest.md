```mermaid
flowchart TB
  Client["浏览器 / 客户端"]
  Post["POST /api/ingest/jobs<br/>multipart: file, auto_convert=true"]
  Api["app/api/ingest.py<br/>创建任务接口"]
  Check{"文件名、扩展名、<br/>auto_convert 是否有效？"}
  Save["分块保存上传文件<br/>llm-wiki-agent/raw/uploads/&lt;安全文件名&gt;"]
  Sig["校验 MIME、大小、部分格式签名/容器"]
  Job["MySQL: ingest_jobs<br/>status=queued, stage=uploaded, progress=0"]
  Queue["进程内 Queue<br/>单个 daemon worker"]
  Poll["GET /api/ingest/jobs/{job_id}<br/>读取任务状态与结果"]

  Client --> Post --> Api --> Check
  Check -- "不通过" --> Reject["422 / 409，不建任务"]
  Check -- "通过" --> Save --> Sig
  Sig -- "不通过" --> Reject
  Sig -- "通过" --> Job --> Queue
  Job -. "202 Accepted" .-> Client
  Client -. "轮询" .-> Poll

  Queue --> Run["worker: _run_job()<br/>MySQL 标记 running"]
  Run --> Kind{"源文件是 .md？"}

  Kind -- "是" --> ReadMd["直接读取 raw/uploads/&lt;name&gt;.md 全文"]
  Kind -- "否，auto_convert=true" --> Convert["MarkItDown 转换<br/>生成同目录 &lt;name&gt;.md"]
  Convert --> ReadMd
  Kind -- "否，auto_convert=false" --> Reject

  ReadMd --> Context["构造 LLM 输入"]
  Context --> Prompt["app/prompts/ingest.md<br/>Template 渲染"]
  Prompt --> Call["app/llm_config.py<br/>LiteLLM completion()"]
  Call --> Length{"finish_reason=length？"}

  Length -- "是" --> Failed["MySQL: status=failed<br/>保留 extracting / 35%<br/>不写 Wiki"]
  Length -- "否" --> Parse["解析 JSON<br/>Pydantic: IngestLLMResult"]
  Parse -- "JSON/结构不合法" --> Repair["非截断时：一次 JSON 修复请求"]
  Repair --> Parse
  Parse -- "仍失败" --> Failed

  Parse -- "合法" --> Lock["取得共享 wiki_lock"]
  Lock --> Write["原子写入 Wiki 文件"]
  Write --> Validate["检查断链、未索引页"]
  Validate --> Success["MySQL: succeeded<br/>stage=completed, progress=100"]
  Success --> Publish{"配置了 PublishService？"}
  Publish -- "是" --> PublishQueue["加入 Quartz 发布批次"]
  Publish -- "否" --> Done["完成"]
  PublishQueue --> Done
```