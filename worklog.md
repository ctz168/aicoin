---
Task ID: 1
Agent: main
Task: 创建详细的部署指南和投票选定模型说明文档

Work Log:
- 检查项目当前状态，确认所有文件在 /home/z/my-project/aicoin/ 目录
- 确认 wallet.py、node.py、run.py 等文件已完成
- 阅读所有核心模块源码（governance.py、blockchain.py、config.py、api_gateway.py、node.py、wallet.py）
- 使用 docx-js 生成专业 Word 文档，采用 Midnight Code 配色方案
- 文档包含封面、目录、8个数据表格、三大部分内容
- 运行 add_toc_placeholders.py 添加 TOC 占位符
- 将文档放入 docs/ 目录并推送到 GitHub

Stage Summary:
- 生成文件: docs/AICoin_部署指南_投票说明.docx
- GitHub 提交: 4938c42
- 文档结构: 封面 → 目录 → 部署指南(8节) → 投票说明(5节) → 常见问题(3节)

