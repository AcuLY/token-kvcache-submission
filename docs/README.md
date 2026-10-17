# 仓库与文档导航

本项目提供双 Agent 会话、Token/KV 上下文交接、模拟网络配置与传输成本选择。

## 从哪里开始

1. [复现与打包](reproduce.md)：依赖、Mock/真实模型启动、演示配置和 ZIP 打包。
2. [共享上下文协议](shared-context-v2.md)：增量范围、校验、提交和成本口径。
3. [真实模型服务](real-model-service.md)：接口、KV 格式与运行配置。

## 目录职责

| 目录 | 内容 |
| --- | --- |
| `shared/` | 合同、公共提示词、Token/KV 编码和校验 |
| `coordinator/` | 会话协调、成本选择、API、事件和模型连接 |
| `agent_peer/` | Agent 控制、模型客户端、P2P 数据传输 |
| `model_service/` | Mock 与 HuggingFace 后端、模型会话和 KV 导入导出 |
| `frontend/` | Vue 界面、前端依赖锁、单元及浏览器测试 |
| `tests/` | Python 单元与运行栈集成测试 |
| `scripts/` | 启动、验证、服务器部署和代码打包入口 |
| `examples/debate/` | 无账号密钥的辩论配置、网络配置与轮次模板 |
| `dist/` | 生成的交付 ZIP，不纳入 Git；不提交虚拟环境或依赖缓存 |
