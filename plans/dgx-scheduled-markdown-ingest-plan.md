# DGX 定时增量 Markdown 入库计划

## 1. 目标与已确认边界

在 DGX 上每天凌晨 03:00 自动扫描 `/home/A/`，把新增的 `.md` 文件通过现有
`wiki-backend` Ingest 流程入库到 Wiki。

已确认规则：

- 首次运行递归处理 `/home/A/` 下已有的全部 Markdown 文件。
- 后续只处理从未被该同步任务记录过的文件；已修改、移动、重命名、删除的文件均不处理。
- 同名但位于不同相对目录的文件分别处理，例如 `a/report.md` 与 `b/report.md`。
- 递归处理普通目录和普通文件；不跟随、也不处理符号链接。
- 扫描开始后才创建或仍在写入的文件留待下一日，避免读取不完整内容。
- 单个文件失败时立即重试一次；第二次仍失败后标记为最终失败并记录告警，以后不自动重试。
- Ingest 历史必须明确显示任务来自“人工上传”还是“定时同步”。

本计划不修改 `llm-wiki-agent` 源码，不开放 `8081`，不新增 FRP 隧道；生产后端继续监听
`127.0.0.1:8081`。

## 2. 设计选择

定时任务不直接执行 Ingest 的内部写 Wiki 方法，而是：

```text
systemd timer
  -> 后端自带同步命令扫描 /home/A/
  -> 读取/更新 MySQL 同步清单
  -> 经 127.0.0.1:8081 调用现有 POST /api/ingest/jobs
  -> 轮询 GET /api/ingest/jobs/{job_id}
  -> 成功或最终失败后写回同步清单
```

这样 Wiki 写入锁、异步 worker 和 Quartz 自动发布仍由已运行的 FastAPI 服务统一拥有；同步命令
只负责扫描、去重、一次重试与日志。该命令只访问本机 loopback API，不增加公网入口。

## 3. 数据模型与幂等性

### 3.1 Ingest 历史来源

为 `ingest_jobs` 增加向后兼容列：

```sql
trigger VARCHAR(32) NOT NULL DEFAULT 'manual'
```

固定枚举：

```text
manual       # Quartz UI/API 的人工上传，旧记录回填此值
scheduled    # /home/A/ 的 systemd 定时同步
```

同步更新：

- `IngestJobResponse` 新增 `trigger`。
- `IngestStorage.create_ingest_job()` 接收 `trigger`，默认 `manual`，保持现有上传 API 兼容。
- `MySQLStorage.initialize()` 为已有表补列并回填默认值。
- `/api/ingest/jobs` 与 `/{job_id}` 的 OpenAPI 文档说明来源含义。

### 3.2 外部源文件同步清单

新增 `scheduled_ingest_sources` 表，不能用 `ingest_jobs.source_path` 代替。后者只记录后端
`raw/uploads/` 中的上传副本，无法区分 `/home/A/` 的相对路径、文件名冲突和最终失败状态。

最小字段：

```sql
id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT
source_key CHAR(64) NOT NULL UNIQUE          -- source_root + NUL + relative_path 的 SHA-256
source_root VARCHAR(500) NOT NULL
relative_path VARCHAR(1000) NOT NULL
source_device BIGINT UNSIGNED NOT NULL
source_inode BIGINT UNSIGNED NOT NULL
state VARCHAR(32) NOT NULL                 -- processing/succeeded/failed
first_seen_at DATETIME NOT NULL
last_attempt_at DATETIME NOT NULL
finished_at DATETIME NULL
ingest_job_id BIGINT UNSIGNED NULL
attempt_count TINYINT UNSIGNED NOT NULL     -- 最大为 2
last_error VARCHAR(1000) NULL
```

唯一身份为配置的源根目录加 Linux 相对路径；不以文件名、内容哈希、mtime 或大小去重。这样完全
符合“只处理从未出现过的文件”：文件内容随后被修改仍不重入库；不同目录的同名文件分别入库。
`source_key` 只用于避免 MySQL `utf8mb4` 长路径联合唯一索引超出键长限制，原始根目录和相对路径
仍完整保存用于审计。另以 Linux `(source_device, source_inode)` 建立唯一身份：同一文件在 `/home/A/`
内改名或移动时 inode 不变，因此会被识别为已处理，而不会作为新文件重复入库。

最终状态：

- `succeeded`：该路径已成功写入 Wiki，以后跳过。
- `failed`：两次尝试都失败，以后跳过并保留错误审计。
- `processing`：本次运行占用。下一次运行或服务异常恢复时不直接重复写入；先根据关联
  `ingest_job_id` 查询任务终态，再归并为 `succeeded` 或 `failed`。若进程在持久化 job ID 前中断，
  无法可靠判断 POST 是否已经成功；该记录会保守地标记为 `failed` 并告警，不会自动重传造成重复入库。
  若关联 job 丢失或到下一次同步仍未终态，同样标记为最终失败并告警。

## 4. 同步命令流程

新增后端自有命令，例如：

```bash
.venv/bin/python -m app.scheduled_ingest
```

配置仅提供受控根目录和本机 API 地址：

```env
WIKI_BACKEND_SCHEDULED_INGEST_ROOT=/home/A
WIKI_BACKEND_SCHEDULED_INGEST_API_URL=http://127.0.0.1:8081
```

命令执行流程：

1. 校验根目录存在、是目录，且实际路径与配置值一致；拒绝根目录自身为符号链接。
2. 递归枚举普通 `.md` 文件，跳过所有符号链接、非普通文件、无读取权限项；按相对路径稳定排序。
3. 仅选择同步清单中不存在的路径和文件身份；首次运行自然选择所有文件。
4. 每个候选文件在上传前记录大小和修改时间，复制到受控临时快照后再次核对源文件元数据。若文件在
   复制期间发生变化，删除快照、不创建同步清单记录，并记录“文件仍在写入，延后处理”；该路径留到
   下一日重新扫描。
5. 对稳定快照先原子创建 `processing` 清单记录，再上传至 loopback `POST /api/ingest/jobs`，并传入
   `trigger=scheduled` 审计标签。该标签由定时命令设置，用于历史展示；当前 API 没有身份认证，不能将
   该字段当作访问控制依据。
6. 轮询对应任务直至 `succeeded` 或 `failed`。成功则清单记为 `succeeded`。
7. 上传、轮询或 Ingest 失败时立即再完整尝试一次；第二次失败将清单记为 `failed`，保留简短错误，
   记录 `WARNING`/`ERROR` 日志并继续下一个文件。
8. 全部文件处理完毕后记录汇总：扫描数、新增数、成功数、最终失败数、跳过数；命令以非零退出码
   表示本批存在最终失败，使 `systemd` 日志与告警系统可发现异常。

不把已发现但尚未写完的文件强制“稳定窗口”处理；由于没有可靠的写入方完成信号，首次扫描未包含的
文件按已确认约定留至次日。复制期间发生变化的文件也不算已处理，避免把不完整内容入库；源文件发布
方应以“写临时文件后原子 rename”为最佳实践。

## 5. 上传冲突与安全边界

现有上传落盘路径只由安全文件名生成，同名文件会冲突。为支持不同目录下同名 Markdown，定时同步
上传时必须生成唯一的后端暂存文件名，例如原文件名加随机 UUID；UI 展示和
`original_filename` 仍保留真实文件名。

实现要求：

- 无论首次上传还是重试，使用不同的暂存文件名，避免第二次被旧的 `raw/uploads/` 文件冲突阻断。
- API 不接收客户端绝对路径；相对路径只写入同步清单，不回显到公开 API 响应。
- 定时命令仅允许 `http://127.0.0.1:8081`；拒绝非 loopback URL，避免把 `/home/A/` 文档上传到外部。
- `source_root` 和 `relative_path` 仅写 MySQL 审计与受控日志；UI 只显示原始文件名和来源标签，避免暴露服务器目录。
- 与现有 10 MiB 上传限制保持一致；超过限制的 `.md` 视为一次失败，重试一次后最终失败。

## 6. Quartz 历史展示

截图中“最近 20 项”每条任务增加来源标签：

```text
人工上传
定时同步
```

推荐在状态标识后的同一行显示紧凑标签，不改变现有状态、文件名、页面变更和发布时间信息；列表
筛选可以先保持按状态，来源筛选不是本期必需项。

Quartz 仅消费新增的 `trigger` 字段：

- 缺失字段兼容显示“人工上传”，保障旧后端联调。
- `manual` 显示“人工上传”。
- `scheduled` 显示“定时同步”。

## 7. systemd 部署

新增两个部署示例文件（不提交真实 DGX 用户、路径或服务器私有配置）：

`/etc/systemd/system/wiki-backend-scheduled-ingest.service`：

```ini
[Unit]
Description=Synchronize new Markdown files into wiki-backend
Requires=wiki-backend.service
After=wiki-backend.service

[Service]
Type=oneshot
User=<DGX_USER>
Group=<DGX_GROUP>
WorkingDirectory=<WIKI_BACKEND_DIR>
Environment=HOME=<DGX_HOME>
EnvironmentFile=<WIKI_BACKEND_DIR>/.env
StandardOutput=journal
StandardError=journal
TimeoutStartSec=infinity
ExecStart=<WIKI_BACKEND_DIR>/.venv/bin/python -m app.scheduled_ingest
```

`/etc/systemd/system/wiki-backend-scheduled-ingest.timer`：

```ini
[Unit]
Description=Run wiki Markdown synchronization every day at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
Unit=wiki-backend-scheduled-ingest.service

[Install]
WantedBy=timers.target
```

服务运行用户必须同时拥有 `/home/A/` 的只读权限，以及项目、虚拟环境和 MySQL 所需权限；不以
`root` 运行。`Persistent=true` 使停机错过的触发在系统恢复后补跑一次。

README 增加安装、启用、手动演练、日志和停用步骤：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wiki-backend-scheduled-ingest.timer
systemctl list-timers wiki-backend-scheduled-ingest.timer
sudo systemctl start wiki-backend-scheduled-ingest.service
journalctl -u wiki-backend-scheduled-ingest.service -n 200 --no-pager
```

## 8. 修改范围与测试

计划修改：

| 位置 | 变更 |
|---|---|
| `app/config.py`、`.env.example` | 受控源目录、本机 API URL 配置与校验 |
| `app/schemas/ingest.py` | `trigger` 枚举与响应字段 |
| `app/api/ingest.py` | 内部定时来源参数的受控接收、路由文档 |
| `app/services/ingest_service.py` | 支持来源、唯一暂存文件名，保持人工 API 兼容 |
| `app/services/scheduled_ingest_service.py` | 扫描、去重、调用/轮询 loopback API、一次重试和日志 |
| `app/scheduled_ingest.py` | systemd 调用的命令入口 |
| `app/storage/mysql.py` | 两张表、兼容补列、清单原子占用/完成状态方法 |
| `tests/test_ingest_service.py`、`tests/test_ingest_api.py` | 来源字段与同名文件暂存测试 |
| `tests/test_scheduled_ingest_service.py` | 扫描、首次全量、增量、符号链接、重复、失败重试、断点恢复 |
| `README.md` | 配置、systemd runbook、来源字段与失败告警说明 |
| Quartz chats 插件 | 历史列表来源标签与兼容显示 |

必须验证：

1. 首次扫描嵌套目录的所有普通 `.md` 文件，生成 `scheduled` Ingest 历史。
2. 第二次扫描不重复上传成功或最终失败路径。
3. 新增文件夹和文件只导入新的相对路径；不同目录同名文件都可导入；改名或移动但 inode 未变化的
   文件不会重复入库。
4. 符号链接、非 Markdown、空文件、越界路径和超限文件不会上传；复制期间发生变化的文件会延后至
   次日，且不会留下去重记录。
5. 任一失败仅提交两次；第二次后为最终失败且下次运行跳过。
6. 服务/系统中断后的 `processing` 清单记录不会自动重传并造成重复 Wiki 写入；无法确认的请求会成为
   可审计的最终失败。
7. 人工上传仍默认为 `manual`，现有 API 与 UI 行为不回归。
8. Quartz 历史对新旧后端响应均可正常显示来源。
9. Windows 完整单元测试通过；DGX ARM64 上完成隔离根目录和隔离 Wiki 的端到端演练。

## 9. 完成标准

- 每日 03:00 的 `systemd timer` 可稳定触发并能从漏跑中恢复。
- `/home/A/` 仅新增普通 Markdown 文件被递归入库，且不跟随链接。
- 去重与最终失败状态持久化在 MySQL，服务重启后保持有效。
- 每个失败文件最多尝试两次，最终失败有可追踪日志，不会每天重复消耗模型资源。
- Ingest 历史与 Quartz 列表能准确区分“人工上传”和“定时同步”。
- 未改变后端公网暴露边界，未泄露服务器源目录或文档正文。
