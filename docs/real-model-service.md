# 模型服务

本文说明模型服务的接口、KV 格式和运行方式。

## 已实现的接口

Model Service 的 HTTP 合同保持不变，只增加后端选择：

```text
MODEL_BACKEND=mock          # 默认，确定性 Mock Token 与 Mock KV，无需 torch
MODEL_BACKEND=huggingface   # 真实因果 LM、真实 tokenizer 与 KV Tensor
```

`model_service/factory.py` 在进程启动时选择后端；`model_service/hf_runtime.py` 实现
`HuggingFaceKvModelRuntime`，提供与 Mock 相同的端点：`describe`、`create_session`、
`tokenize`、`render_local_input`、`prefill`、`generate`、`seal`、`cache_status`、
`evict_cache`、`import_tokens`，以及 KV 的 `export_cache`/`import_cache`。

真实后端使用分块 Prefill、`logits_to_keep=1` 与 greedy/sampled 生成；KV 导出为每层
`[k, n)` 的真实 Tensor，导入后按序列位置拼接并续推。所有 forward 由一把锁串行化，
因为只有一份模型权重和一个设备。

## KV 线上格式

Mock 的每位置 16 字节块不再写死。`RuntimeDescriptor` 声明：

| 字段 | Mock | HuggingFace |
| --- | --- | --- |
| `kv_cache_format` | `mock-kv-v1` | `hf-stacked-kv-v1` |
| `kv_bytes_per_token` | `16` | `2 × layers × kv_heads × head_dim × itemsize` |
| `kv_container_bytes` | `0` | 固定 32 字节头 |

KV payload = uint32 Token sidecar + 容器头 + 原始 Tensor bytes，因此
`payload_bytes = (n - k) × (4 + kv_bytes_per_token) + kv_container_bytes`。
`coordinator/planning.py`、`shared/contracts.py` 的 `TransferPlan` 校验和
`agent_peer/handlers.py` 的 `KvCacheHandler` 都使用同一公式，真实 Tensor 大小可直接
进入计划与 P2P 传输。

容器头是自定义固定格式（magic、dtype code、layers、batch、heads、seq、head_dim），
不是 `torch.save` 的 pickle 头。原因是 pickle 头会随 storage size 跨越整数编码边界
而漂移 1–2 字节，导致计划与实际 payload 长度不一致；固定头保证两端字节数完全可预测。

## 本机运行真实后端

```powershell
uv pip install -r scripts/hf-model-service-requirements.txt
$env:HF_MODEL_ID='Qwen/Qwen2.5-0.5B-Instruct'
$env:HF_DTYPE='float32'
$env:HF_DEVICE='cuda'
uv run python scripts/run_local.py --model-backend huggingface
```

或只启动模型服务：`MODEL_BACKEND=huggingface HF_MODEL_ID=... python -m model_service`。
`--model-backend mock` 仍是不需要 torch 的默认路径。

## 使用服务器上的模型

推荐在本机原生运行应用，在「环境」页选择 SSH 或 HTTP 模型连接。SSH 使用本机已有配置、密钥和已信任的主机指纹；应用管理隧道和有限重试。
完整步骤见 [复现指南](reproduce.md)。Docker 默认用于 Mock 演示，不自动具备宿主机 SSH 配置或 Windows 网卡选择能力。

以下保留命令行入口：

Coordinator 与 Agent 只需要一个 HTTP base URL，因此服务器模型通过 SSH 端口转发接入：

```powershell
# 在服务器启动真实后端并转发到本机 8100，再用 external 模式启动本机栈
uv run python scripts/model_service_remote.py --host gpu-box --remote-dir ~/token-kvcache `
    --start-remote --model Qwen/Qwen2.5-1.5B-Instruct --dtype bfloat16 --device cuda --with-stack

# 服务器已启动时只建隧道
uv run python scripts/model_service_remote.py --host gpu-box --tunnel-only

# 只在本机跑真实后端，不用 SSH
uv run python scripts/model_service_remote.py --local --model Qwen/Qwen2.5-0.5B-Instruct
```

`--with-stack` 使用 `scripts/run_local.py --model-backend external --model-service-url
http://127.0.0.1:<local-port>`；`run_local.py` 的 `external` 模式不会启动本地模型进程，
只校验 `<url>/health` 后启动 Coordinator 与两个 Agent。

服务器侧命令在仓库目录内以 `MODEL_BACKEND=huggingface` 启动 `python -m model_service`，
日志写入 `model-service.log`；SSH 参数、端口与远端 Python 都可通过命令行覆盖。

## 测试入口

- `tests/unit/test_hf_runtime.py`：小型随机 Llama 的 KV 布局、导入导出、续推一致性和缓存覆盖。
- `tests/unit/test_kv_wire.py`：payload 公式、计划校验、拆包和非法尺寸拒绝。
- `tests/unit/test_remote_model_service.py`、`test_model_factory.py`：SSH 命令构造与后端选择。
- `tests/integration/test_live_stack.py`：四服务栈的协议与会话流程。

## 提示词格式与解码

真实后端默认按模型自己的 chat template 组织回合，`HF_PROMPT_FORMAT` 控制：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | tokenizer 带 `chat_template` 时用模板，否则回退纯文本 |
| `chat_template` | 强制模板；tokenizer 没有模板时启动即失败 |
| `plain` | 始终用纯文本标记，使用纯文本轮次模板 |

提示词由三部分构成，分别在哪里改：

| 部分 | 位置 | 是否需重启服务 |
| --- | --- | --- |
| 公共任务、Agent A 提示词、Agent B 提示词（P0 正文） | WebUI「Agent 与配置」，逐会话生效 | 否 |
| 逐轮标记 | 环境变量 `HF_TURN_TEMPLATE`，支持 `{agent_id}`、`{turn_id}` 与 `{system_prompt}`（当前 Agent 的公开提示词） | 是 |
| 回合结构（user/assistant 标记、BOS） | 模型自带的 `chat_template` | 是（换模型） |

改完 P0 后必须**新建会话**：会话创建时会把当时的配置快照下来，运行中的会话不受后续编辑影响。

模板路径把共享 P0 作为第一条 user 消息（用 user 而非 system，因为部分模型族不接受
system 角色），每轮标记渲染成“user 回合 + assistant 头部”，模型接着写出的就是回答本身；
逐轮渲染会剥掉模板自动附带的 BOS，保证规范序列里只有一个 BOS。解码改用
`model.generate`，因此 `repetition_penalty`、`no_repeat_ngram_size`、采样等参数对任何
模型都通用（`HF_REPETITION_PENALTY`、`HF_NO_REPEAT_NGRAM`）。

两个必须处理的细节：传入 `past_key_values` 时 transformers 会算出空的 `cache_position`，
必须显式提供；`generate` 返回时最后一个 token 尚未写入 cache，需要按“缺多少补多少”补齐，
KV 长度不变式才能维持。

## 更新服务器代码

模型服务代码本就在本仓库（`model_service/`）；`scripts/sync_to_server.py` 是配套的更新
路径，替代手工 `scp` 快照：

```bash
python scripts/sync_to_server.py --host gpu-box --restart-service
```

默认用单条 SSH 连接把归档流式送入远端解压并重启服务（适合限制新连接的共享主机），
`--scp` 可切回先上传文件的方式。
