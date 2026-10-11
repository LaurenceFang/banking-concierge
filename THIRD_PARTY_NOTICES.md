# 第三方资料说明

当前版本包含项目自写规格、实现和模拟知识资料，未复制被参考研究项目的源码、模型权重或原始数据集。运行时依赖通过包管理器安装。

Python 依赖及版本见 `pyproject.toml` 和 `uv.lock`，前端依赖及版本见 `web/package.json` 和 `web/package-lock.json`，数据库镜像见 `compose.yaml`。主要组件包括 FastAPI、Pydantic、SQLAlchemy、HTTPX、LangGraph、jieba、React、Vite、Lucide、react-markdown 和 remark-gfm；其许可由各自发行包提供，发布依赖包时需保留相应许可。

论文与作者仓库链接集中在 [来源表](specs/07-论文与开源来源.md)。引用方法与复制实现分别记录；后续引入第三方代码、数据或依赖时，保留其适用许可、署名和版本。项目 MIT 许可不替代第三方许可。

knowledge 目录为自写模拟银行资料，不表示任何银行的真实产品或服务政策。

## 界面字体

正文使用 [霞鹜文楷](https://github.com/lxgw/LxgwWenKai)，按 SIL Open Font License 1.1 分发。原始 TTF 和许可在 `web/public/fonts`，网页专用 WOFF2 分块由 `web/scripts/build-wenkai-web.py` 生成，保留原字体元数据和许可；原始许可明确允许网页专用子集/格式转换继续使用保留名称。

界面标签使用 [霞鹜新晰黑](https://github.com/lxgw/LxgwNeoXiHei)，按 IPA Font License 1.0 分发，字体 TTF 未修改。其上游为 IPAex Gothic，字体工程源见 [上游 projects](https://github.com/lxgw/LxgwNeoXiHei/tree/main/projects)。完整许可、原字体获取及替换步骤随字体附在 `web/public/fonts`。这两款字体不受本仓库 MIT 许可替代。
