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


## 7. 报告与视频中的真实模型实验（2026-10-17）

本轮配置见 `examples/recorded-20261017/session.json`，逐轮数据和汇总分别见同目录的 `transfers.json`、`results.json`。这些记录与报告、视频来自同一批 11 个正式会话，共 48 次成功交接。

模型为 Qwen2.5-7B-Instruct，A10G 24 GB、FP16，`HF_PROMPT_FORMAT=chat_template`、`HF_MAX_CONTEXT_TOKENS=8192`、`HF_PREFILL_CHUNK_TOKENS=8`。所有组使用同一分块设置；模型人为延迟均为 0。协调服务、两个 Peer 和模型服务均运行于同一云端主机，Peer 之间实际 TCP 传输并进行软件网络整形。分块大小会影响补算耗时，因此复现时需要保持一致。

1. 启动真实模型与项目服务后载入上述会话配置。固定 Token、固定 KV 各运行一个 4 次发言的校准会话，以 3 次交接各阶段耗时的中位数更新成本参数，随后冻结。示例文件保存的是本次机器的实测参数；不同机器应重新校准。
2. 静态实验分别设 20、200、10000 Mbps，RTT 1 ms、抖动 0；各运行固定 Token 和固定 KV，均为 4 次发言。比较对齐的首次交接。
3. 动态实验分别运行固定 Token、固定 KV 和自适应，均为 9 次发言。8 次交接带宽依次设为 20、10000、10000、20、20、10000、10000、20 Mbps。在前一次交接提交后、下一次计划生成前更新，并核对导出记录的网络快照。本次自适应实际序列为 Token、KV、KV、Token、Token、KV、KV、Token。
4. 缺口实验固定 KV、200 Mbps、4 次发言。首次交接后暂停，在 Agent A 驱逐 64 或 128 个历史尾部缓存位置，再继续；0 缺口采用静态 KV 200 Mbps 会话的第二次交接。核对驱逐命令完成、缓存覆盖 254 降至 190 或 126、后续交接提交与继续生成。

完整交接耗时包括准备、传输确认与接收恢复。每种配置仅运行一个正式会话，本轮自适应累计 2622.222 ms，较固定 Token 的 3117.060 ms 减少 15.88%，较固定 KV 的 10997.786 ms 减少 76.16%。这组结果对应上述分块设置与网络整形条件。代码中的成本比较规则未为得到混合选择而修改。
