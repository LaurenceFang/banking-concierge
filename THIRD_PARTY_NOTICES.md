# 第三方资料说明

当前版本包含项目自写规格、实现和模拟知识资料，未复制被参考研究项目的源码、模型权重或原始数据集。运行时依赖通过包管理器安装。

Python 依赖及版本见 `pyproject.toml` 和 `uv.lock`，前端依赖及版本见 `web/package.json` 和 `web/package-lock.json`，数据库镜像见 `compose.yaml`。主要组件包括 FastAPI、Pydantic、SQLAlchemy、HTTPX、LangGraph、jieba、React、Vite、Lucide、react-markdown 和 remark-gfm；其许可由各自发行包提供，发布依赖包时需保留相应许可。

论文与作者仓库链接集中在 [来源表](specs/07-论文与开源来源.md)。引用方法与复制实现分别记录；后续引入第三方代码、数据或依赖时，保留其适用许可、署名和版本。项目 MIT 许可不替代第三方许可。

knowledge 目录为自写模拟银行资料，不表示任何银行的真实产品或服务政策。
