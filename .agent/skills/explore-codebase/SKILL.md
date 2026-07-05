---
name: explore-codebase
description: 快速摸清一个陌生代码库的结构与关键模块
when_to_use: 接手新项目、或需要在陌生代码库里定位功能时
---

# 探索代码库 SOP

1. 先 list_dir 看顶层结构，找到入口 / 配置文件（README、package.json、requirements.txt、*.csproj 等）。
2. 读入口文件和 README，理解项目是做什么的、用了什么技术栈。
3. 用 grep 按关键词（函数名 / 类名 / 业务术语）定位感兴趣的模块。
4. 读关键模块，梳理"数据怎么流、谁调用谁"。
5. 输出：一句话项目概览 + 3~5 个最关键文件及其职责。
