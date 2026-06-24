---
name: data-analyzer
description: 分析多格式数据文件并生成统计、图表和报告。
version: 1.0.9
dependencies:
- pandas
- openpyxl
- python-docx
- pymupdf
- matplotlib
category: data
---
# Data Analyzer

多格式数据分析工具，支持 Excel、CSV、Word、PDF、TXT、Markdown 文件的读取、统计、对比和报告生成。

## 功能特性

- 📊 **多格式支持**: Excel、CSV、Word、PDF、TXT、Markdown
- 📁 **文件夹扫描**: 自动扫描并分类文件夹内所有支持的文件
- 📈 **统计分析**: 支持求和、平均值、趋势计算
- 🔄 **数据合并**: 合并多个文件的数据
- 📉 **可视化**: 生成图表
- 📋 **多格式导出**: Markdown、Excel、Word、PDF
- 🌍 **多语言**: 支持中英文输出

## 使用方法

### 基本用法

```python
from data_analyzer import DataAnalyzer

# 初始化分析器
analyzer = DataAnalyzer("/path/to/folder")

# 扫描文件夹
files = analyzer.files  # {'excel': [], 'csv': [], 'word': [], 'pdf': [], 'txt': [], 'markdown': []}

# 分析单个文件
result = analyzer.analyze_file("/path/to/file.xlsx")

# 生成汇总报告
summary = analyzer.generate_summary()
```

### 分析 Excel 文件

```python
result = analyzer.analyze_excel("data.xlsx")
# 返回: {'rows': N, 'columns': M, 'data': DataFrame}
```

### 分析 CSV 文件

```python
result = analyzer.analyze_csv("data.csv")
# 返回: {'rows': N, 'columns': M, 'data': DataFrame}
```

### 分析 Word 文件

```python
result = analyzer.analyze_word("document.docx")
# 返回: {'paragraphs': N, 'text': '文本内容'}
```

### 分析 PDF 文件

```python
result = analyzer.analyze_pdf("document.pdf")
# 返回: {'pages': N, 'text': '文本内容'}
```

## 代码参考

完整代码请参考 {{SKILL_DIR}}/templates/data_analyzer.py

## 依赖安装

```bash
pip install pandas openpyxl python-docx pymupdf matplotlib
```

## 注意事项

- 本地处理，数据不上传
- 支持跨平台运行 (macOS, Linux, Windows)
- 支持 .xlsx, .xls, .csv, .docx, .pdf, .txt, .md, .markdown 格式
