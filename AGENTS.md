# Windows + DGX Spark 开发与部署工作流规范

> 适用场景：  
> Windows 作为主开发机，使用 Codex Desktop / Claude CLI / OpenCode / VS Code / Git 编写和修改代码；  
> NVIDIA DGX Spark 作为远程 Linux Ubuntu ARM64 运行机，用于 `uv` / Python 服务、Ollama、本地模型、FRP/Nginx、GPU 推理和最终部署验证。
> `wiki-backend` 当前优先使用 DGX 宿主机原生 `uv` 部署，先不使用 Docker。

---

## 1. 总体原则

本项目采用以下开发模式：

```text
Windows = 主开发机 / 代码源 / Agent 工作区
DGX Spark = Linux ARM64 运行机 / uv Python 服务机 / GPU 算力机 / 部署环境
```

核心规则：

1. **代码主要在 Windows 上修改。**
2. **Codex Desktop、Claude CLI、OpenCode 等 Coding Agent 主要运行在 Windows 上。**
3. **DGX Spark 上只存放同步后的项目代码、`uv` 虚拟环境、模型服务、数据库、FRP/Nginx 等运行组件。**
4. **DGX Spark 原则上不直接手工修改业务代码。**
5. **最终运行环境以 DGX Spark 的 Linux Ubuntu ARM64 为准。**
6. **所有脚本、路径、换行符、权限、Python 依赖和运行命令都必须兼容 Linux Ubuntu ARM64。**

---

## 2. 机器职责划分

### 2.1 Windows 主开发机

Windows 上负责：

- 项目源码主仓库
- VS Code / Cursor 等编辑器
- Codex Desktop
- Claude CLI
- OpenCode
- Git
- Chrome / Edge 浏览器
- 文档编写
- 代码生成、重构、审查
- Git commit / push
- 通过 SSH 远程触发 DGX Spark 部署

推荐目录：

```text
C:\Users\10421\projects\
```

例如：

```text
C:\Users\10421\projects\wiki_backend
C:\Users\10421\projects\research_report_library
C:\Users\10421\projects\llm-wiki-agent
```

---

### 2.2 DGX Spark 远程运行机

DGX Spark 上负责：

- Ubuntu / DGX OS 运行环境
- ARM64 Linux 部署验证
- Python / uv / venv
- Docker / Docker Compose（后续需要容器化时再启用，当前 `wiki-backend` 不作为首选部署方式）
- Ollama / Open WebUI / vLLM / llama.cpp
- CUDA / NVIDIA Container Runtime
- 项目服务运行
- 数据库 / Redis / 向量库
- FRP / Nginx / 内网穿透
- 日志查看
- GPU 推理和模型服务

推荐目录：

```text
/home/<user>/projects/
/home/<user>/models/
/home/<user>/datasets/
/home/<user>/data/
/home/<user>/docker/
/home/<user>/logs/
```

例如：

```text
/home/user/projects/wiki_backend
/home/user/projects/research_report_library
/home/user/projects/llm-wiki-agent
```

---

## 3. 不要两边混合开发

必须遵守：

```text
Windows 改代码
→ Git 提交
→ Git 推送
→ DGX Spark 拉取
→ DGX Spark 构建和运行
```

不要长期这样做：

```text
Windows 改一部分
DGX Spark 改一部分
两边各有一份不同版本
```

这会导致：

- 代码不一致
- Git 冲突
- 部署版本混乱
- Agent 无法判断真实源码
- Linux 上临时修复遗失
- Windows 与 Linux 行为不一致

---

## 4. 标准工作流

### 4.1 日常开发流程

在 Windows 上：

```powershell
cd C:\Users\10421\projects\wiki_backend

# 使用 VS Code / Codex Desktop / Claude CLI / OpenCode 修改代码

git status
git add .
git commit -m "update backend"
git push
```

在 DGX Spark 上：

```bash
ssh user@192.168.x.x

cd ~/projects/wiki_backend
git pull

# 创建虚拟环境.venv
uv venv --python 3.12
# 安装依赖
uv pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
# 启动项目
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081
```

浏览器访问：

```text
http://192.168.x.x:<服务端口>
```

---

### 4.2 推荐部署流程

推荐让 Windows 触发远程部署。

Windows PowerShell 示例：

```powershell
$server = "user@192.168.x.x"
$project = "/home/user/projects/wiki_backend"

git status
git add .
git commit -m "deploy update"
git push

ssh $server "cd $project && git pull && uv venv && uv pip install -r requirements.txt && .venv/bin/python -m unittest discover -s tests"
ssh $server "tmux has-session -t wiki-backend 2>/dev/null && tmux kill-session -t wiki-backend || true"
ssh $server "cd $project && tmux new-session -d -s wiki-backend '.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081'"
```

如果不想每次自动提交，可以拆成两步：

```powershell
git push
ssh user@192.168.x.x "cd ~/projects/wiki_backend && git pull && uv venv && uv pip install -r requirements.txt && .venv/bin/python -m unittest discover -s tests"
```

---

## 5. DGX Spark 上不建议安装的东西

DGX Spark 不建议作为普通桌面开发机使用。

通常不建议安装：

- Codex Desktop
- Chrome GUI
- VS Code GUI
- 多个 Coding Agent
- 大量 Windows 风格桌面工具
- 无关的全局 npm 包
- 混乱的 Python 发行版
- 随意安装的 CUDA / cuDNN / PyTorch wheel

DGX Spark 应该保持干净，专注于：

- Linux 服务运行
- Python / uv / venv
- Docker（后续容器化阶段再使用）
- Ollama
- GPU 推理
- 项目部署

---

## 6. DGX Spark 上建议安装的基础组件

DGX Spark 推荐具备：

```bash
sudo apt update
sudo apt install -y \
  git curl wget unzip ca-certificates gnupg lsb-release \
  build-essential pkg-config \
  openssh-server \
  tmux htop nvtop
```

Docker / Compose（当前 `wiki-backend` 先不使用 Docker；后续容器化时再检查）：

```bash
docker --version
docker compose version
```

Python / uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Ollama：

```bash
ollama --version
```

FRP / Nginx 按项目需要安装。

---

## 7. Linux Ubuntu ARM64 优先原则

虽然代码在 Windows 上开发，但最终运行环境是：

```text
Linux Ubuntu ARM64
```

因此所有代码和配置必须优先兼容：

- Linux 路径
- Linux 换行符 LF
- Linux 文件权限
- Linux shell 脚本
- Linux `uv` / `.venv` Python 运行环境
- ARM64 架构
- NVIDIA GPU / CUDA / Container Runtime

不能只按 Windows 本地能跑作为完成标准。

---

## 8. 路径规范

### 8.1 不要写死 Windows 路径

避免：

```python
path = "C:\\Users\\10421\\projects\\wiki_backend\\data\\input.txt"
```

推荐：

```python
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
data_path = BASE_DIR / "data" / "input.txt"
```

---

### 8.2 配置通过环境变量或 `.env` 传入

`wiki-backend` 的配置项定义在 `app/config.py`，真实机器配置写在项目根目录 `.env` 中。不要为了适配某台机器去改 `config.py`。

当前项目实际读取的 `.env` 示例：

```env
WIKI_AGENT_REPO_PATH=../llm-wiki-agent
WIKI_BACKEND_MYSQL_HOST=127.0.0.1
WIKI_BACKEND_MYSQL_PORT=3306
WIKI_BACKEND_MYSQL_USER=wiki_backend_app
WIKI_BACKEND_MYSQL_PASSWORD=replace-with-real-password
WIKI_BACKEND_MYSQL_DATABASE=wiki_backend
WIKI_BACKEND_DEFAULT_CHAT_TITLE=新对话
WIKI_BACKEND_CHAT_HISTORY_LIMIT=6
```

DGX Spark 上的真实 `.env` 单独维护，不提交 Git。`WIKI_AGENT_REPO_PATH` 应使用 Linux 相对路径或绝对路径，不能使用 Windows 反斜杠路径。

---

## 9. 换行符规范

### 9.1 统一使用 LF

项目内文本文件应统一使用 Linux 换行符：

```text
LF
```

不要使用 Windows 默认的：

```text
CRLF
```

尤其是这些文件必须使用 LF：

```text
*.sh
Dockerfile
docker-compose.yml
*.yml
*.yaml
*.env.example
*.py
*.toml
*.md
```

如果 shell 脚本使用 CRLF，在 DGX Spark 上可能报错：

```text
bad interpreter: /bin/bash^M: no such file or directory
```

---

### 9.2 推荐添加 .gitattributes

在项目根目录添加：

```gitattributes
* text=auto eol=lf

*.bat text eol=crlf
*.cmd text eol=crlf
*.ps1 text eol=crlf

*.sh text eol=lf
*.py text eol=lf
*.md text eol=lf
*.yml text eol=lf
*.yaml text eol=lf
*.toml text eol=lf
Dockerfile text eol=lf
.env.example text eol=lf
```

说明：

- Linux/Docker 相关文件强制 LF。
- Windows 专用脚本 `.bat`、`.cmd`、`.ps1` 可以保留 CRLF。
- 业务代码优先 LF。

---

### 9.3 Windows Git 推荐设置

在 Windows 上推荐：

```powershell
git config --global core.autocrlf input
git config --global core.eol lf
```

解释：

- `core.autocrlf input`：提交时把 CRLF 转成 LF，检出时不强制转回 CRLF。
- `core.eol lf`：默认使用 LF。
- 这更适合最终部署到 Linux 的项目。

查看当前配置：

```powershell
git config --global --get core.autocrlf
git config --global --get core.eol
```

---

### 9.4 VS Code 推荐设置

项目根目录添加：

```text
.vscode/settings.json
```

内容：

```json
{
  "files.eol": "\n",
  "files.insertFinalNewline": true,
  "files.trimTrailingWhitespace": true,
  "editor.formatOnSave": true
}
```

如果不想提交 `.vscode/settings.json`，也可以只在本地工作区设置。

---

## 10. 脚本执行权限规范

Linux shell 脚本必须具备执行权限。

如果后续新增脚本，例如：

```bash
chmod +x scripts/deploy.sh
chmod +x scripts/start.sh
chmod +x scripts/entrypoint.sh
```

提交权限变化：

```bash
git add scripts/deploy.sh
git commit -m "make deploy script executable"
```

确认 Git 记录了执行权限：

```bash
git ls-files -s scripts/deploy.sh
```

如果权限正确，应该看到类似：

```text
100755
```

而不是：

```text
100644
```

---

## 11. Shell 脚本规范

Shell 脚本第一行必须写：

```bash
#!/usr/bin/env bash
```

推荐模板：

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Starting service..."
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081
```

避免使用 Windows PowerShell 语法写 Linux 脚本。

---

## 12. wiki-backend 部署规范：uv 优先

### 12.1 当前不使用 Docker

`wiki-backend` 当前先部署为 DGX Spark 宿主机上的原生 Python 服务：

- 使用 `uv` 创建和维护项目内 `.venv`。
- 使用 `requirements.txt` 安装依赖。
- 使用 `.venv/bin/python` 执行测试和启动 FastAPI。
- 暂不编写或依赖 `Dockerfile` / `docker-compose.yml` 作为首选部署路径。

```bash
cd ~/projects/wiki_backend
git pull
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081
```

---

### 12.2 DGX 上的运行前置条件

DGX Spark 上必须确认：

```bash
uname -m
uv --version
.venv/bin/python --version
mysql --version
```

`uname -m` 应为 ARM64 架构，例如 `aarch64`。

`.env` 必须在 DGX Spark 上单独维护，不提交 Git。示例：

```env
WIKI_AGENT_REPO_PATH=../llm-wiki-agent
WIKI_BACKEND_MYSQL_HOST=127.0.0.1
WIKI_BACKEND_MYSQL_PORT=3306
WIKI_BACKEND_MYSQL_USER=wiki_backend_app
WIKI_BACKEND_MYSQL_PASSWORD=replace-with-real-password
WIKI_BACKEND_MYSQL_DATABASE=wiki_backend
WIKI_BACKEND_DEFAULT_CHAT_TITLE=新对话
WIKI_BACKEND_CHAT_HISTORY_LIMIT=6
```

`WIKI_AGENT_REPO_PATH` 在 DGX 上应使用 Linux 相对路径或绝对路径，不能使用 Windows 路径。

MySQL 需要提前创建数据库和用户。如果 `.env` 中 `WIKI_BACKEND_MYSQL_HOST=127.0.0.1`，建议同时授权 `localhost` 和 `127.0.0.1`：

```sql
CREATE DATABASE wiki_backend
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'wiki_backend_app'@'localhost'
  IDENTIFIED BY 'replace-with-a-strong-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wiki_backend.* TO 'wiki_backend_app'@'localhost';

CREATE USER 'wiki_backend_app'@'127.0.0.1'
  IDENTIFIED BY 'replace-with-a-strong-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wiki_backend.* TO 'wiki_backend_app'@'127.0.0.1';

FLUSH PRIVILEGES;
```

---

### 12.3 服务启动与健康检查

前台验证：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081
```

后台运行可先使用 `tmux`：

```bash
tmux new-session -d -s wiki-backend \
  'cd ~/projects/wiki_backend && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081'
```

验证：

```bash
curl --fail --silent --show-error http://127.0.0.1:8081/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats
```

Windows 浏览器访问：

```text
http://192.168.x.x:8081/health
```

`/health` 只验证 FastAPI 进程可用；`/api/chats` 成功才说明 MySQL 配置基本可用。若 Quartz 前端通过 `http://192.168.x.x:8080` 访问后端，需要同步更新 `app/main.py` 的 CORS `allow_origins`。

---

### 12.4 Docker 后续启用条件

只有当 `uv` 原生部署已在 DGX Spark 上验证通过后，才考虑 Docker 化。

后续如果重新启用 Docker，必须重新验证：

- ARM64 基础镜像是否可用。
- `markitdown[all]`、`litellm`、`PyMySQL` 等依赖是否能在 ARM64 镜像内安装。
- 容器内 `.env`、MySQL、Ollama、`llm-wiki-agent` 路径是否可达。
- 启动后 `/health`、`/api/query`、chat、ingest、synthesis 端到端路径是否正常。

---

## 13. Python 规范

推荐 Python 项目结构：

```text
project/
├─ app/
├─ data/
├─ scripts/
├─ tests/
├─ requirements.txt
├─ .env.example
├─ .gitignore
└─ README.md
```

当前 `wiki-backend` 使用 `requirements.txt` 作为依赖入口，尚未使用 `pyproject.toml` / `uv.lock`。后续如果迁移到 `pyproject.toml`，需要同步更新 README、AGENTS 和部署命令。

推荐使用：

```bash
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

不要提交：

```text
.venv/
__pycache__/
*.pyc
.env
```

---

## 14. .gitignore 推荐

```gitignore
# Python
.venv/
__pycache__/
*.pyc
*.pyo
*.pyd
.pytest_cache/
.mypy_cache/
.ruff_cache/

# Env
.env
.env.local
.env.*.local

# Logs
logs/
*.log

# Data / runtime
data/runtime/
tmp/
.cache/

# Node
node_modules/
dist/
build/

# OS / editor
.DS_Store
Thumbs.db

# Optional: do not ignore project-level VS Code settings if they enforce LF
# .vscode/
```

如果 `.vscode/settings.json` 用于统一 LF、格式化规则，可以提交。  
如果只是个人偏好，可以忽略。

---

## 15. Git 同步规范

Windows 是主代码源。

Windows 上：

```powershell
git status
git add .
git commit -m "message"
git push
```

DGX Spark 上：

```bash
cd ~/projects/<project>
git pull
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081
```

DGX Spark 上不要直接开发。  
如果必须临时修改，应立即：

```bash
git diff
```

然后把改动同步回 Windows 或提交到 Git 仓库，避免丢失。

---

## 16. 推荐部署脚本

### 16.1 Windows 端 deploy-dgx.ps1

```powershell
$ErrorActionPreference = "Stop"

$server = "user@192.168.x.x"
$project = "/home/user/projects/wiki_backend"

Write-Host "Checking git status..."
git status

Write-Host "Pushing latest code..."
git push

Write-Host "Deploying on DGX Spark..."
ssh $server "cd $project && git pull && uv venv && uv pip install -r requirements.txt && .venv/bin/python -m unittest discover -s tests"
ssh $server "tmux has-session -t wiki-backend 2>/dev/null && tmux kill-session -t wiki-backend || true"
ssh $server "cd $project && tmux new-session -d -s wiki-backend '.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081'"
ssh $server "curl --fail --silent --show-error http://127.0.0.1:8081/health"
ssh $server "curl --fail --silent --show-error http://127.0.0.1:8081/api/chats"
```

---

### 16.2 DGX 端可选 scripts/deploy.sh

当前仓库尚未提交 `scripts/deploy.sh`。如果后续需要 DGX 本机部署脚本，可按下面模板新增：

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

git pull
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests

if tmux has-session -t wiki-backend 2>/dev/null; then
  tmux kill-session -t wiki-backend
fi

tmux new-session -d -s wiki-backend \
  'cd ~/projects/wiki_backend && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081'

curl --fail --silent --show-error http://127.0.0.1:8081/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats
```

赋权：

```bash
chmod +x scripts/deploy.sh
git add scripts/deploy.sh
git commit -m "add dgx deploy script"
```

---

## 17. Ollama / 模型服务规范

Ollama 推荐运行在 DGX Spark 上。

Windows 只作为访问入口：

```text
Windows Chrome
→ http://192.168.x.x:3000
→ Open WebUI on DGX Spark
```

当前 `wiki-backend` 不直接读取 `OLLAMA_BASE_URL`；LLM 调用由 `llm-wiki-agent` 的配置负责。如果后续需要在相关模型配置中指定 DGX 宿主机 Ollama，原生运行时通常使用：

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

如果后续改为 Docker 容器运行，再改为：

```env
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

当前 `wiki-backend` 先不运行在 Docker 容器内；后续如果容器化，再配置：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

---

## 18. FRP / Nginx 规范

DGX Spark 可以作为内网服务运行机。

常见结构：

```text
DGX Spark 服务
├─ backend: 8081
├─ frontend: 8080
├─ ollama: 11434
├─ open-webui: 3000
└─ frpc / nginx
```

原则：

1. 外部访问只暴露必要端口。
2. 不要直接暴露 Ollama 11434 到公网。
3. 对外服务尽量通过 Nginx 做统一入口。
4. FRP 配置文件不要提交真实 token。
5. 真实域名、token、密码放 `.env` 或服务器私有配置中。

---

## 19. Codex Desktop 使用注意事项

Codex Desktop 运行在 Windows，默认只能直接访问 Windows 本地项目文件。

因此：

- Codex Desktop 应主要操作 Windows 本地仓库。
- 不要让 Codex Desktop 假设它可以直接读写 DGX Spark 上的文件。
- 如果需要修改 DGX 上的代码，应先修改 Windows 仓库，再通过 Git 同步到 DGX。
- 如果需要分析 DGX 上的报错，应把日志复制到 Windows，或通过 SSH 命令获取日志后交给 Codex 分析。
- 所有部署相关建议必须考虑 Linux Ubuntu ARM64 运行环境。

Codex 在生成命令时，应优先生成 Linux 兼容命令，而不是 Windows 专用命令，除非任务明确发生在 Windows 本地。

---

## 20. 给 Coding Agent 的重要上下文

当 Codex Desktop、Claude CLI、OpenCode 处理本项目时，必须记住：

```text
本项目在 Windows 上开发，但最终运行在 NVIDIA DGX Spark 上。
DGX Spark 是 Linux Ubuntu / DGX OS 环境，CPU 架构是 ARM64。
所有项目代码、shell 脚本、Python 依赖、路径和权限，都必须以 Linux Ubuntu ARM64 为最终标准。
Windows 只作为主开发机和 Agent 工作区。
不要生成只适用于 Windows 的路径、命令或部署方式，除非用户明确要求。
当前 `wiki-backend` 部署建议优先使用 DGX 宿主机 `uv` + `.venv`，不要默认生成 Docker 部署方案。
```

---

## 21. 常见问题与处理

### 21.1 shell 脚本在 DGX 上报 `/bin/bash^M`

原因：

```text
脚本是 CRLF 换行符
```

处理：

```bash
sed -i 's/\r$//' scripts/*.sh
chmod +x scripts/*.sh
```

根本解决：

- 使用 `.gitattributes` 强制 LF。
- VS Code 设置 `"files.eol": "\n"`。
- Windows Git 设置 `core.autocrlf input`。

---

### 21.2 DGX 上提示 permission denied

例如：

```text
permission denied: ./scripts/start.sh
```

处理：

```bash
chmod +x scripts/start.sh
git add scripts/start.sh
git commit -m "fix script execute permission"
```

---

### 21.3 uv / Python 在 Windows 能跑，DGX 上不能跑

优先检查：

```bash
uname -m
uv --version
.venv/bin/python --version
.venv/bin/python -m unittest discover -s tests
curl --fail --silent --show-error http://127.0.0.1:8081/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats
```

常见原因：

- pip 包不支持 ARM64
- 脚本权限不对
- 换行符是 CRLF
- 路径写死为 Windows 路径
- 依赖了 Windows-only 工具
- CUDA / GPU 相关依赖不兼容
- `.env` 中的 `WIKI_AGENT_REPO_PATH` 仍是 Windows 路径
- DGX 上 MySQL 用户、密码、数据库或权限未配置

---

### 21.4 Python 路径问题

避免：

```python
open("C:\\Users\\10421\\data\\a.txt")
```

推荐：

```python
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
file_path = BASE_DIR / "data" / "a.txt"
```

---

### 21.5 环境变量问题

不要在代码里写死密钥和地址。

推荐：

```python
import os

ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
```

`.env.example` 提供模板，真实 `.env` 不提交。

---

## 22. 最终执行标准

一个任务只有满足以下条件，才算完成：

1. Windows 本地代码已修改完成。
2. Git 状态清晰。
3. 代码已 commit / push。
4. DGX Spark 已 pull 到最新代码。
5. DGX Spark 上已通过 `uv venv` 和 `uv pip install -r requirements.txt` 安装依赖。
6. DGX Spark 上 `.venv/bin/python -m unittest discover -s tests` 通过。
7. DGX Spark 上 `.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081` 能成功启动。
8. DGX Spark 上 `/health` 返回成功。
9. DGX Spark 上日志无明显错误。
10. Windows 浏览器能访问 DGX Spark 上的服务。
11. 所有脚本在 Linux Ubuntu ARM64 上可执行。
12. 没有 CRLF 导致的 Linux 执行错误。
13. 没有写死 Windows 路径。
14. `.env`、MySQL、`llm-wiki-agent` 路径在 DGX 上配置正确。
15. 敏感配置未提交到 Git。

---

## 23. 一句话总结

本项目的长期工作流是：

```text
Windows 负责编码、Agent 修改、Git 管理；
DGX Spark 负责 Linux ARM64 运行、uv Python 服务、Ollama/模型服务、FRP/Nginx 和最终验证。
```

所有代码和配置都必须以：

```text
Linux Ubuntu ARM64 on DGX Spark
```

作为最终兼容标准。
