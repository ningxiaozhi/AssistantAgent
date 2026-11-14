---
name: DDIA 第 4 章 编码与演化
description: 数据编码格式的兼容性取舍，回答"为什么事件用 Protobuf 而日志用 JSON"时用
type: learn
tags: [分布式, 序列化]
updated: 2026-11-12
sources: [inbox/2026-11-10-ddia-ch4.pdf]
---

## 结论

Schema 演化有两条路：**向后兼容**（新代码读旧数据）与**向前兼容**（旧代码读新数据）。
Avro 靠 writer/reader schema 解决，Protobuf 靠字段编号 + 可选字段。

## 我的用法

- 事件表：Protobuf（体积小、演进友好）
- 日志：JSON（可读性优先，检索方便）

## 相关

[[DDIA 第 3 章 存储引擎]]
