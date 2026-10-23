# Docker Desktop 本次启动修复

2026-10-24。本机Docker Desktop 4.59.0启动时先后无法移除两个残留AF_UNIX套接字重解析点：`%LOCALAPPDATA%/Docker/run/dockerInference`和`%LOCALAPPDATA%/docker-secrets-engine/engine.sock`。原始日志报ERROR_CANT_ACCESS_FILE及路径语法错误，不能仅由最后一句推断Windows不支持该路径。

确认Docker进程退出后，仅将对应运行目录改名保留，让应用重建；没有删除、factory reset、清卷、注销WSL或修改VPN。保留目录：

- `%LOCALAPPDATA%/Docker/run.before-repair-20261024-021803`
- `%LOCALAPPDATA%/Docker/run.before-repair-20261024-021938`
- `%LOCALAPPDATA%/docker-secrets-engine.before-repair-20261024-021938`

第二次启动后Docker服务端返回29.2.0，项目原有`ai_concierge-postgres`卷可见。`docker compose up -d --wait postgres`成功，banking-concierge-db健康。部分用户已有容器按Docker自身重启策略随引擎恢复，本次未更改这些策略或操作其业务。

这是本次恢复，不保证后续重启不复发。没有为验证复发再次重启整个Docker，避免打断恢复的其他服务。类似问题见[Docker项目问题记录](https://github.com/docker/desktop-feedback/issues/531)，该记录不是永久修复保证。

随后仅在专用concierge_test执行PG用例；初轮15通过、3工作流失败，继续诊断及结果见SPEC31。应用concierge库与SQLite演示库未重置。
