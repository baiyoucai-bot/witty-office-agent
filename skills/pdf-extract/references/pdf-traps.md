# PDF 抽取边界

- **文字层 vs 扫描件**：有文字层才能抽。扫描件 `pdftotext` 成功但 0 字，check 报 FAIL，走 `ocr.py`。
- **OCR 有误差**：RapidOCR 认出来的金额、编号必须人工核对，不要直接当 `ledger.toml` 的 text。
- **加密**：文件头附近出现 `/Encrypt` 就停，不猜密码。
- **版式**：`pdftotext -layout` 尽量保栏，仍会丢页眉页脚位置。表格走 `tables.py`（pdfplumber），对不齐再人工。
- **合并 / 填表**：`compose.py` 只拼接页面或改 AcroForm 域值。没有域的扫描件填不了。不从零排版新 PDF。
- **老格式**：`.doc` 不是 PDF，走 `office-document` 的 `convert_legacy.py`。
