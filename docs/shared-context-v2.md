# 共同上下文增量通信 v2

## 已实现的语义

两个 Agent 使用相同模型、共同初始 P₀ 和统一的对话 Token 序列，分别持有模型 Session。
它们是同一逻辑上下文的不同执行进度副本。P₀ 同时包含任务、两个角色和共享背景，双方
独立 Prefill 后校验相同 Token digest、长度和 runtime fingerprint，初始化耗时单列。

默认模板每轮添加统一的 Agent/turn 标记、生成输出与结束标记。辩论示例模板使用
`{system_prompt}` 再次加入当前 Agent 的公开提示词，以强化角色约束；这些 Token 计入
`wrapper_token_count`。接收端原样追加同一规范序列，不能替换为另一套模板或省略包装 Token。

| 正常交接 | 发送内容 | 接收行为 |
| --- | --- | --- |
| TOKEN/DELTA | 本轮输出 α + 统一包装标记 | 在现有历史上增量 Prefill，保留已知历史 |
| KV/DELTA | 实际缺失的 KV 区间 + 校验 sidecar | 按位置追加，补齐缺口，不重复追加已知 Token |
| KV/FULL | 没有有效缓存或换 epoch 时的完整缓存 | 校验公共 P₀、状态与格式后恢复 |
| TOKEN/RECOVERY | 明确的 checkpoint/Token 历史恢复快照 | 校验公共 P₀ 后恢复；不会被标作正常输出消息 |

P₀ 已经在双方建立，因此正常 KV 会话从首次交接即可使用 DELTA。

## 两套覆盖范围与缺口

CacheInventory 分别记录 `known_state_id / known_token_count / known_token_digest` 和
实际 KV 的 `token_count / prefix_digest`。同一 prefix 的缓存不因生成节点不同而失效。

正常 Token 增量 Prefill 同样产生有效 KV，覆盖应推进；因此连续 Token 不必然积累 ξ。
实际 KV 被淘汰时：

- Token 路径仅发送新内容，接收端使用已有历史 Token 补算缺失缓存，再追加新内容。
- KV 路径从实际缓存水位开始发送，包含历史缺口和本轮新增区间。
- 已知 Token 的重复 sidecar 只校验，不重复追加。目标长度与 digest 必须对应共同状态。

测试通过 Agent 的 `evict_cache` 控制命令实际裁去缓存尾部，支持 `retain_prefix_tokens`。
旧的“每传 Token 自动清空 KV”策略已移除，不能靠模式名制造缺口。

## 窗口与协议

`history_window_tokens` 包括公共 P₀、对话历史、下一轮标记及输出预算。超限时固定保留
P₀，保留可用对话预算的一半作为近端历史，重算后推进 epoch。较旧对话上下文会丢弃，
UI 与 CSV 记录截断数和恢复原因。如果 P₀ 加本轮预算已放不下，明确返回 CONTEXT_LIMIT。

正常交接不使用 `prefill(replace=True)`；该接口只用于初始化等显式操作。Token 消息导入
使用 `/v2/import-tokens`。数据面 protocol_version 为 2，Peer 要求对应的 V2 能力，旧版
Agent 不能与新版会话混接。KV 格式由 RuntimeDescriptor 声明：Mock 为 `mock-kv-v1`，真实后端为 `hf-stacked-kv-v1`；不等同于控制/数据协议版本。

模型导入在检查全部前缀、长度、epoch 和目标 digest 后再改状态。相同 transfer_id 的
同内容导入幂等；冲突重试拒绝。Peer 保存已接收 manifest、payload digest 与 ACK，重复
交接不会重新追加，也不会把已经继续生成的状态回滚到旧消息。中心只在成功 ACK 后更新
账本，ACK 未确认时不宣称中心提交完成；持久化故障续跑仍不在当前实现内。

## 三种成本口径

`paper` 为代码默认目标，实现论文 Eq. (1)、(18)、(19)。配置包含 L、H、d_h、d_m、d_f、
d_v、接收端 FLOP/s、Token 位宽、KV 精度和压缩倍率。FP16 时 KV 的理论 bit 数为
`32 × L × H × d_h × (ξ + α) / γ`。

`mode_e2e` 和 `p2p_only` 分别比较工程交接与实际协议链路预测。后者使用实际 JSON frame
长度、当前后端的 payload 大小、带宽和 RTT；模型调用开销是可校准的估计，并非自动学习结果。

| 字段 | 含义 |
| --- | --- |
| `output_token_count` / `current_tokens` | 本轮纯输出 α |
| `wrapper_token_count` / `wrapper_tokens` | 额外角色/结束标记 |
| `theta_tokens` | 接收端在当前规范 epoch 已知的历史长度 |
| `xi_tokens` | 发送端当前输出之前、接收端缺失的兼容 KV 长度 |
| `paper_bits` / `paper_ms` | 论文配置下的理论量和边际耗时 |
| `payload_bytes` / `frame_bytes` | 当前后端的实际协议负载和完整帧 |
| `prefill_tokens` | Token 工程路径实际需计算的位置数，包含真正的缓存缺口 |
| `effective_objective` | 本次实际使用的比较口径 |
| `handoff_e2e_ms` | 被选路径实测的准备至接收恢复耗时 |

当历史 KV 缺失或需要恢复 checkpoint 时，Eq. (18) 的正常增量 Prefill 假设不成立。
此时仍保存理论值，但将 `paper_applicable=false`，明确用工程成本比较并记录原因。
未选模式没有虚构实测。公开的模型维度只用于分析；当前 Mock 的每位置 16 字节块不代表
真实 Tensor 大小，也没有实现 γ 所表示的量化/压缩器。

## 测试入口

协议及运行栈测试见 `tests/unit/` 和 `tests/integration/test_live_stack.py`，覆盖增量交接、前缀校验、幂等重试、缓存缺口与窗口恢复。

可选的真实注意力 Tensor 验证：

```powershell
uv pip install -r scripts/hf-validation-requirements.txt
.\.venv\Scripts\python.exe scripts/validate_hf_cache.py
```

脚本使用 CPU 上随机初始化的小型 Llama，不下载预训练权重；比较 Token Prefill 与 KV 追加后的续推 logits，并测试 Tensor 经 TCP 传输后的恢复。

模型后端与运行配置见 [真实模型服务](real-model-service.md)。模型会话与协议注册保存在内存中，重启后不恢复旧会话。
