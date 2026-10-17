# 从干净环境复现与打包

## 1. 依赖与目录

在仓库根目录执行命令。基础环境：Python 3.11/3.12、uv；前端推荐 Node.js 24、pnpm 10.14.0。`uv.lock` 和 `frontend/pnpm-lock.yaml` 固定基础依赖。HuggingFace/GPU 依赖为可选环境，需按实际 CUDA 条件配置，不由基础锁文件提供。

```powershell
uv sync --frozen
corepack enable
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..
```

首次安装需要访问依赖源；ZIP 不包含已下载依赖、模型权重或前端构建产物。

## 2. 无 GPU：Mock 功能复现

```powershell
uv run --frozen python scripts/run_local.py --port 18090 --model-backend mock
```

打开 http://127.0.0.1:18090 。此模式可验证页面、协议、网络整形和状态机，但回复和 KV 块为确定性模拟，不能用来声称真实模型推理成功。终端 Ctrl+C 结束本次启动的进程。

也可运行 `docker compose up --build --wait`，打开 http://localhost:8080 。默认 Compose 是 Mock 环境；容器不会自动取得宿主机 SSH 密钥。

## 3. 真实模型：独立服务 + 原生应用

模型服务器与本机都需要本仓库的兼容代码。推荐在服务器自己的隔离目录创建虚拟环境：

```sh
uv sync --frozen --no-dev
uv pip install --python .venv/bin/python -r scripts/hf-model-service-requirements.txt
export MODEL_BACKEND=huggingface
export HF_MODEL_ID='/absolute/path/to/Qwen2.5-3B-Instruct'
export HF_LOCAL_FILES_ONLY=true
export HF_DEVICE=cuda:0
export HF_DTYPE=bfloat16
export HF_MAX_CONTEXT_TOKENS=8192
export HF_PROMPT_FORMAT=chat_template
export HF_TURN_TEMPLATE="$(cat examples/debate/turn-template.txt)"
export MODEL_SERVICE_HOST=127.0.0.1
export MODEL_SERVICE_PORT=18100
.venv/bin/python -m model_service
```

这是 Linux/bash 示例，模型路径和 GPU 需按自己环境填写；示例不下载权重。依赖文件使用 torch 2.8.0+cu128、transformers 4.57.6，其他驱动/CUDA 组合需自行验证。不要安装进系统 Python 或结束其他用户的进程。模型权重需合法自行取得。

本机完成步骤 1、2，待两个 Agent READY 后，到「环境」选择 SSH 模式，填写自己已有的 SSH 主机别名及端口 18100，点击「连接并使用」。密钥和已验证的主机指纹由自己的 SSH 配置提供，不随代码包分发。Windows 受代理影响时，可选择当前 WLAN 地址；网卡地址变化后需重新选择。

连接成功后确认模型为 Qwen/HuggingFace；不能仅凭页面可打开判断已切换真实模型。也支持直接填 HTTP 模型 URL；浏览器中的 localhost 是本机，容器中的 localhost 是容器自身。

在 Windows 本机运行真实模型时，用 PowerShell 的 `$env:变量='值'` 设置上面相同变量；模板用 `$env:HF_TURN_TEMPLATE=(Get-Content examples/debate/turn-template.txt -Raw).Trim()`。可选 HF 依赖必须安装到启动模型服务使用的同一个 Python 环境。

## 4. 载入演示参数

可以直接在页面编辑提示词和网络设置；完整可复现参数位于 `examples/debate/`。若用 API 导入，先结束活动会话，在 PowerShell 运行：

```powershell
$demoBase = 'http://127.0.0.1:18090/api/v1'
$demoCurrent = Invoke-RestMethod "$demoBase/config"
$demoConfig = Get-Content examples/debate/session.json -Raw -Encoding utf8 | ConvertFrom-Json
$demoBody = @{config=$demoConfig; expected_version=$demoCurrent.version} | ConvertTo-Json -Depth 30
Invoke-RestMethod "$demoBase/config" -Method Put -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($demoBody))
$demoNetwork = Get-Content examples/debate/network.json -Raw -Encoding utf8 | ConvertFrom-Json
$demoNetwork | Add-Member -NotePropertyName version -NotePropertyValue (Invoke-RestMethod "$demoBase/mock").version
$demoBody = $demoNetwork | ConvertTo-Json -Depth 20
Invoke-RestMethod "$demoBase/mock" -Method Put -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($demoBody))
```

刷新页面，确认六次发言、自适应策略、4096 Token 历史窗口、关闭 EOS 结束整场会话，然后新建会话。检查六条消息与五条 `COMMITTED`，点开 Token/KV 标记查看真实 payload、原因与耗时。理论目标的维度是场景参数，不代表当前模型准确性能；网络配置也不代表物理网卡速率。

## 5. 运行测试

```powershell
uv run --frozen pytest tests/unit -q
cd frontend
pnpm lint
pnpm test
pnpm build
cd ..
```

HF 相关测试依赖 torch/transformers，未安装时部分测试会跳过；不要把跳过当作通过。独立数值验证入口及其依赖见 `scripts/validate_hf_cache.py`、`scripts/hf-validation-requirements.txt`。浏览器与运行栈集成测试入口见根 README，可能新建会话，应在空闲测试栈执行。

重启 Coordinator 会清空内存配置和会话，运行实验后应保存导出结果。

## 6. 生成源码归档

```powershell
python scripts/package_submission.py
```

打包不依赖第三方库，也不要求先提交 Git。它读取当前工作区选定的源代码、依赖锁、文档和示例配置，生成 `dist/token-kvcache-source-<UTC时间>.zip`。ZIP 内 `SUBMISSION-MANIFEST.json` 列明文件、大小、基础提交及是否有未提交改动。

文档采用明确白名单，仅包含 `docs/README.md`、`docs/reproduce.md`、`docs/shared-context-v2.md` 和 `docs/real-model-service.md`。

不包含 `.git`、`.env`、SSH 密钥、模型权重、虚拟环境、依赖缓存、构建产物、测试缓存、连接配置导出、设计草案、阶段记录、实验数据、设计图、作者交流截图及报告材料。其他文档保留在原工作区，不进入源码归档。

解压后从步骤 1 开始。Mock 可在无 GPU 环境复现；真实模型需要另行配置模型与 GPU 资源。ZIP 是可复现的源码快照；打包不会执行上传、Git 提交或推送。
