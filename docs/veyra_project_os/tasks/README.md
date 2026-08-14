# Task System

## 规则

`CURRENT.md` 是执行窗口唯一默认任务。Roadmap、ADR 和 Architecture 不能自动授权实现。

每个任务必须：

- 是一个纵向、可验收切片；
- 有具体 User Outcome；
- 说明 Why Now；
- 指定必须阅读的文档章节；
- 限定 Scope 与 Non-goals；
- 声明 Authority Delta；
- 区分 automated/live/user evidence；
- 使用 [`../definition_of_done.md`](../definition_of_done.md)；
- 只有在证据闭合、状态达到 `COMPLETED` 后才归档；`CURRENT.md` 中的稳定任务
  ID 必须保留在归档文件名中。`LC-NNN-title.md` 只适用于稳定 ID 本身为
  `LC-NNN` 的 Living Context 任务，不把 `V0-001` 重新编号成 `LC-001`。
- 归档后再创建新的 `CURRENT.md`。如果当前任务仍是
  `LOCAL_VALIDATED / OWNER_ACCEPTANCE_PENDING`，它继续留在 `CURRENT.md`；
  Handoff 中提到的下一条 `LC-001` 只是计划，不得提前替换当前任务。

## 任务不能包含

- 整个 Roadmap；
- “顺手完成”多个阶段；
- 未经证据的大规模重构；
- 把 proposed Architecture 当作已授权施工；
- 没有用户结果却宣称产品完成；
- 自动合并 main、扩大权限或接入外部账号。

## 执行窗口 Prompt

```text
先核验共享工作树，不覆盖其他窗口改动。
阅读 Project OS README、CURRENT 及其引用章节。
只完成 CURRENT。遇到会改变 User Outcome、authority 或数据来源的决定时停止并报告。
按 Definition of Done 验收；无法回答用户第一次感受到什么时标记 INFRASTRUCTURE。
```
