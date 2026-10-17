# Token/KV Cache 通信实验台

[观看实验演示（10 分 26 秒）](videos/demo.mp4) · [下载视频](https://raw.githubusercontent.com/AcuLY/token-kvcache-submission/main/videos/demo.mp4)

视频包含静态带宽对照、动态策略选择、缓存驱逐与恢复三个实验，配有中文旁白和画内字幕。

双 Agent 通信系统，支持共同上下文上的 Token 增量与 KV 全量/增量传输。Coordinator 负责控制面
与 WebUI，两个 Agent 通过 P2P TCP 传输状态，独立 Model Service 默认提供确定性的 Mock
接口，也可切换为 HuggingFace 真实后端（本机 GPU 或经 SSH 隧道访问服务器模型）；
Mock Network Adapter 对实际字节流施加带宽、RTT 与抖动。

- [仓库与文档导航](docs/README.md)
- [从干净环境复现与打包](docs/reproduce.md)
- [可直接载入的辩论配置](examples/debate/session.json)

源码归档：`python scripts/package_submission.py`（包含当前未提交源代码，输出到 `dist/`）。

## 快速启动

需要 Docker Desktop/Linux Docker Engine 和 Docker Compose v2。启动前确认 Docker engine
正在运行，且可访问 Docker Hub、GHCR、PyPI 和 npm registry。

```sh
docker compose up --build --wait
```

打开 <http://localhost:8080>。在“Agent 与配置”确认两端 READY，编辑并保存初始任务、
两份 Profile 和发言上限；返回“会话”，点击“新会话并开始”。默认生成 6 条回复，
进行 5 次 P2P 交接。默认策略为自适应 Token / KV；在配置页选择“始终 KV（优先增量）”
后，两端先独立建立相同公共 P₀，正常交接从第一次就可使用 KV/DELTA。
自适应默认按论文理论边际成本比较；工程交接和 P2P 目标也可选择。点击“Mock 实验室”可修改双向带宽、RTT、抖动和模型延迟。

默认宿主机端口：WebUI/Coordinator `8080`、Mock Model `8100`、Agent 控制面
`8201/8202`、P2P `9101/9102`。8080 可用 `COORDINATOR_HOST_PORT` 环境变量覆盖。
例如 PowerShell：`$env:COORDINATOR_HOST_PORT='8088'`，然后运行 Compose。

```sh
docker compose ps
docker compose logs --tail=50 coordinator agent-a agent-b
docker compose stop
```

所有状态只保存在内存。浏览器刷新通过快照和事件续传恢复；Coordinator 重启后历史、
配置和正在运行的会话清空。暂停/停止在当前生成或交接的安全边界生效，不提供故障续跑。

## 运行与数据边界

- 四个独立服务角色：Coordinator、Model Service、Agent A、Agent B。默认模型为 Mock；真实模型演示把 Model Service 部署在服务器，两个 Agent 仍在本机。
- Coordinator 提供 HTTP/WebSocket、权威状态账本和 Token/KV 固定及自适应策略；仅接收文本观测和
  metadata，不接收规范 Token ID 数组或转发 P2P payload。
- 协议 v2 的正常 Token 交接只传新增输出与统一标记（TOKEN/DELTA），使用大端 uint32。
  接收端在自身规范历史上追加 Prefill；TOKEN/RECOVERY 仅用于明确标记的 checkpoint/恢复，
  不再将完整 Token 快照当作普通消息。
- `mock-byte-v1` 是 UTF-8 byte tokenizer；演示回复是确定性 Mock 输出，不是真实 LLM。
  A/B 使用同一 RuntimeDescriptor，但各自持有隔离的模型 Session。
- Mock 网络对实际 TCP frame 施加一次 RTT（含固定 seed 的抖动）和分块限速；计划绑定
  网络版本。修改网络参数只影响后续签发的计划。模型延迟更新影响后续模型调用。
- Token 的 `payload_bytes=4×(n-known_tokens)`；Mock KV 的 `payload_bytes=20×(n-cached_tokens)`，
  包含每个位置 16 字节模拟 KV block 和 4 字节 Token sidecar。TCP framing 和 JSON manifest
  另计入 `frame_bytes`。
  因此实测会包含 framing、HTTP 与调度开销，不应要求与简化预测毫秒级相等。

## 页面

| 页面 | 内容 |
| --- | --- |
| 会话 | 会话记录、控制按钮、宽对话气泡，向下滚动查看当前交接与实时观测 |
| Agent 与配置 | 自动注册事实、Profile、下一会话默认值；运行中的配置快照不受编辑影响 |
| 实验分析 | Token/KV 理论与工程成本、网络、阶段耗时、交接明细、事件和 CSV |
| 环境与适配器 | 当前 provider 与能力声明，未来能力保持明确不可用状态 |
| Mock 实验室 | 由 Adapter 声明注册的工具页；网络参数与模型行为分标签编辑 |

Cache 页与候选成本表区分本轮输出 α、包装标记、接收端历史 θ、缺失 KV ξ，以及 Token
已知范围和 KV 驻留范围。两端共同初始化 P₀ 后，正常连续 Token Prefill 会登记兼容 KV，
不人为制造 debt。发生缓存淘汰时，Token 模式用已知历史本地补算，KV 模式直接补传缺失块。

## 共享上下文 v2 与论文成本

- P₀ 包含任务与两个 Agent 的公开提示词。默认逐轮只追加发言标记与输出；服务器辩论模板额外使用 `{system_prompt}` 重申当前角色。该内容属于双方共享的规范序列，计入 `wrapper_token_count` 和真实传输负载，并非私有上下文。
- 模型 Token 追加接口 `/v2/import-tokens` 校验 base/target digest、runtime、epoch 与消息去重；
  KV 模型接口继续使用 `/v1/cache-status`、`/v1/export-cache`、`/v1/import-cache` 的版本化合同。
- 默认 `paper` 目标实现论文 Eq. (1)、(18)、(19)：α-only 传输与在 θ 上的 Prefill，
  KV 理论量按层数、头数、维度、精度和 γ 计算。配置中的模型参数用于理论分析，不代表已部署该模型。
- 工程 `mode_e2e` 和 `p2p_only` 使用真实协议大小及明确的调用开销估计；未选模式没有虚构实测。
  缓存缺口重算或恢复不满足论文正常路径假设时，记录 `effective_objective=mode_e2e` 与原因。
- Mock 后端使用每位置 16 字节的确定性块；HuggingFace 后端传输真实 KV Tensor。理论 Tensor 量与实际 wire 量始终分列，真实格式由 RuntimeDescriptor 声明。
- `history_window_tokens` 默认 1024，可填空关闭。窗口重建固定保留公共 P₀，并保留较近的
  对话尾部，推进 epoch。**会丢弃较旧的对话上下文**；已知 Token 与缓存不能跨 epoch 混用。
- 已取消自动“Token 发送即释放 KV”的旧策略；故障注入可通过 Agent 的 `evict_cache` 控制命令
  显式释放全部或尾部 KV，保留 Token，测试会记录实际覆盖变化。

初始化、正常增量、缺口补齐与恢复分别统计。新旧协议 Agent 不得混接。现有内存会话不跨
服务重启迁移；升级前应导出旧历史，旧版上下文接管结果不可冒充 v2 实验。

协议说明见 [共享上下文 v2](docs/shared-context-v2.md)。

## 接入真实模型服务

Model Service 通过 `MODEL_BACKEND` 选择后端，默认 `mock`，HTTP 合同与端口不变：

| 后端 | 用途 | 关键配置 |
| --- | --- | --- |
| `mock`（默认） | 确定性 Token/Mock KV，无需 torch | 无 |
| `huggingface` | 真实因果 LM、真实 tokenizer 与 KV Tensor | `HF_MODEL_ID`、`HF_DTYPE`、`HF_DEVICE`、`HF_PREFILL_CHUNK_TOKENS`、`HF_MAX_CONTEXT_TOKENS`、`HF_PROMPT_FORMAT`、`HF_REPETITION_PENALTY` |

真实后端把实际 KV Tensor 大小写入 `RuntimeDescriptor.kv_bytes_per_token`，把固定序列化头
写入 `kv_container_bytes`；Coordinator 与 Agent 据此计算 `payload_bytes`，P2P 上传输的是
模型导出的真实 KV bytes，而不是服务器内部的 Cache handle。

本机运行真实后端（需要 torch/transformers，见 `scripts/hf-model-service-requirements.txt`）：

```powershell
$env:HF_MODEL_ID='Qwen/Qwen2.5-0.5B-Instruct'; $env:HF_DTYPE='float32'; $env:HF_DEVICE='cuda'
uv run python scripts/run_local.py --model-backend huggingface
```

本机已有 `.venv/gpu` 和缓存的 Qwen2.5-1.5B-Instruct 时，可离线启动：

```powershell
./scripts/run_local_model.ps1
```

该入口使用本机 CUDA、FP32 和模型自带的 chat template，不连接 SSH 或下载权重。
打开 <http://127.0.0.1:18080/agents>，编辑公共任务和双方各自的一份提示词，
身份、立场、背景和回复要求统一写入对应 Agent 的提示词；生成参数默认折叠。
旧配置的角色和背景会合并显示在提示词中，保存时清空旧字段，避免隐藏要求残留。
保存后到“会话”点击“新会话并开始”。建议每次回复上限 256 tokens、共享历史窗口
8192 tokens，并关闭“遇到 EOS 时停止”，让一条回复自然结束后仍能交给另一个 Agent。
发言次数可先设为 4，温度可自由修改。修改仅影响新会话；配置和历史仍为内存状态，
重启服务后需重新设置。启动脚本所在终端按 Ctrl+C 停止服务。

在应用的「环境」页可直接连接已有模型服务：选择「实验室服务器 · SSH」，填写本机 SSH 配置别名（如 `token-kv`）和服务器模型端口（如 `18100`），然后点击「连接并使用」。应用管理隧道，并在模型检查通过后切换两个 Agent；不需要手动运行隧道脚本。若代理影响内网连接，可选择 WLAN 网卡直连。

远程大体积 KV 导入与交接确认的默认等待上限为 120 秒，可通过 Agent 的 `REQUEST_TIMEOUT_SECONDS` 调整。模型服务的 `HF_TURN_TEMPLATE` 支持 `{agent_id}`、`{turn_id}` 和 `{system_prompt}`；最后一个字段使用当前会话对应 Agent 的公开提示词，适合需要逐轮明确角色的辩论。修改此环境变量后需重启模型服务。

连接拒绝、握手中断和超时会自动重试，最多 4 次，间隔 5、15、30 秒；页面显示尝试次数和重试倒计时。认证和主机密钥错误直接报告，不重试。失败时保留原模型，切换期间禁止新建会话；已有活动会话需要先结束。提示词和历史记录在切换时保留，但连接配置和会话仍为内存状态，重启服务不保留。服务器需已运行支持共享上下文协议的模型服务，持续不可达时重试无法保证接通。

也可继续使用手动 SSH 隧道入口（可选在服务器上启动后端），再把本机栈指到隧道：

```powershell
uv run python scripts/model_service_remote.py --host gpu-box --remote-dir ~/token-kvcache `
    --start-remote --model Qwen/Qwen2.5-1.5B-Instruct --dtype bfloat16 --device cuda --with-stack
```

`--with-stack` 在隧道就绪后用 `--model-backend external` 启动 Coordinator 与两个 Agent。
已有模型服务时用 `--tunnel-only`，只在本机跑真实后端用 `--local`。

真实模型的接口、KV 格式与配置方式见 [真实模型服务](docs/real-model-service.md)。
Token/KV 数值一致性测试与运行栈测试分别验证缓存恢复和协议行为；语言质量需按具体模型与任务评估。

## 开发与检查

Python 3.11 + uv；Node 24 + pnpm 10.14。版本化依赖由 `uv.lock` 和
`frontend/pnpm-lock.yaml` 固定。

```sh
uv sync
uv run ruff check shared model_service coordinator agent_peer tests scripts
uv run mypy shared model_service coordinator agent_peer
uv run pytest tests/unit -q
```

在 `frontend` 目录：

```sh
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

`test:e2e` 使用独立接口 fixtures 检查布局与状态交互。真实服务验收另行运行：

```powershell
$env:TOKEN_KVCACHE_BASE_URL='http://127.0.0.1:8080'
uv run pytest tests/integration -q
# 以下在 frontend 目录运行
$env:E2E_BASE_URL='http://127.0.0.1:8080'
pnpm exec playwright test -c playwright.live.config.ts
```

真实测试会创建会话并短暂调整 Mock 配置；应对空闲演示栈运行。无窗口浏览器截图保存到
系统临时目录 `token-kvcache-qa`，不操作系统键鼠。

不使用 Docker 的开发启动：先构建前端，再执行 `uv run python scripts/run_local.py`。
同样启动四个独立进程，入口 <http://127.0.0.1:18080>；`--port` 可修改起始端口，
随后连续五个端口用于模型、控制面和 P2P。Ctrl+C 仅结束此脚本启动的进程。

## 后续接入

该脚本复用项目 TCP 数据面验证真实 Token/KV 传输，与网页四服务栈分开运行；不能合并为同一次实验。网页栈目前可选择 Mock 或真实 HuggingFace 后端。

- 模型客户端依赖 `shared/interfaces.py` 中的 `ModelRuntimeClient`；客户端位于
  `agent_peer/model_client.py`，服务端默认 `MockModelRuntime`，可切换 `HuggingFaceKvModelRuntime`。
  KV wire layout 由 `RuntimeDescriptor` 声明（`kv_cache_format`/`kv_bytes_per_token`/
  `kv_container_bytes`），`KvCacheHandler` 与 `planning.py` 按声明计算 payload，不再写死 16 字节/位置。
- 中心通过 `ProviderRegistry` 提供网络/传输能力，`planning.py` 形成范围，`decision.py` 比较成本；
  Session FSM 不按 Agent 角色分支。前端消费 `CapabilityDocument` 和工具页贡献声明。
- Agent 的 `handlers` 注册表负责 payload 编码、解码和模型恢复；`TcpByteTransport`
  只处理 manifest 与 bytes。测试使用额外 JSON Handler 验证无需修改 TCP/FSM 即可传输。
- 每个镜像可独立运行：`python -m coordinator`、`python -m agent_peer`、
  `python -m model_service`。跨机器通过环境变量配置控制面与模型 URL、data bind 和
  data advertise；不依赖 Compose service name 常量。
- Coordinator HTTP bind：`COORDINATOR_HOST/PORT`；模型 bind：`MODEL_SERVICE_HOST/PORT`
  （兼容旧名 `MOCK_MODEL_HOST/PORT`）；
  Agent HTTP bind：`CONTROL_BIND_HOST/PORT`，公告：`CONTROL_ADVERTISE_URL`；
  P2P：`DATA_BIND_HOST/PORT` 与 `DATA_ADVERTISE_HOST/PORT`；模型地址：`MODEL_SERVICE_URL`。

## 常见启动问题

- Docker 命名管道不存在：先在本机确认 Docker engine 已启动。
- `proxyconnect ... 127.0.0.1 ... refused`：Docker daemon 的代理不可达；由机器管理员修正
  Docker 代理配置，应用本身不修改系统代理。
- 容器 healthy 但页面不可达：检查宿主机发布端口是否已被占用，改用
  `COORDINATOR_HOST_PORT`；不要结束无关进程。
- Agent READY 但不能创建会话：查看配置的两个 Agent ID、RuntimeDescriptor 与现有活动会话。
