# 数据许可：CC BY-NC 4.0（非商业）

本目录下的数据文件（`raw/`、`normalized/`）来自 Science Data Bank 数据集
**《2022 年度西藏地质灾害普查数据》**（DOI `10.57760/sciencedb.27813`），
由 **田建（Tian Jian），中国交通科学研究院** 发布，采用
**Creative Commons Attribution-NonCommercial 4.0 International（CC BY-NC 4.0）** 许可。

- 许可全文：<https://creativecommons.org/licenses/by-nc/4.0/legalcode>
- 许可摘要：<https://creativecommons.org/licenses/by-nc/4.0/>

## 你可以

- **署名**：保留作者、标题、来源、许可链接与变更说明；
- **非商业性再分发**：在非商业目的下复制、分发、展示本数据；
- **演绎**：在非商业目的下修改、转换、基于本数据创作（须注明变更）。

## 你不可以

- **商业性使用**：包括但不限于把本数据（或它的演绎物）用于收费产品、商业服务、
  商业性研究赞助项目，或随商业发行物分发；
- **附加限制**：再分发时不得对本数据施加额外的法律或技术限制。

## 与本仓库主许可的关系

本仓库代码采用 **AGPL-3.0**；**本目录的数据不适用 AGPL-3.0**，只适用 CC BY-NC 4.0。
两份许可互不吞并：

- 商用本仓库代码时，**必须移除本目录**（或在 Science Data Bank 另行取得商业授权）；
- 非商业使用时，代码仍按 AGPL-3.0、数据按 CC BY-NC 4.0，各自履行。

## 署名格式

```text
田建（Tian Jian），中国交通科学研究院. 2022 年度西藏地质灾害普查数据[DS/OL]. V1.
Science Data Bank, 2025. https://doi.org/10.57760/sciencedb.27813.
CSTR:31253.11.sciencedb.27813. CC BY-NC 4.0.
```

本目录的规范化文件是对原数据的**格式转换与字段筛选**（GB18030→UTF-8、列选择、
日期 ISO 化、坐标取首点），未改变原始数值；变更说明见 `README.md` 的"规范化口径"。
