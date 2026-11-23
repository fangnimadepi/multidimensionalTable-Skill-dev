"""Validate Office/PDF containers before report extraction."""
import zipfile

def validate_document(path, extension):
    """Inspect the actual container, never rename PDF bytes as a DOCX."""
    if extension == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 10000
                    or sum(v.file_size for v in entries) > 256 * 1024**2
                    or not {"[Content_Types].xml", "word/document.xml"}
                    <= set(archive.namelist())
                ):
                    raise ValueError("DOCX内容不完整或解压后过大。")
                # Parse only; Office macros and embedded objects are never executed.
                from docx import Document

                Document(path)
                return {"format": "docx", "requiresOCR": False}
        except (zipfile.BadZipFile, KeyError) as error:
            raise ValueError("附件内容不是有效DOCX，请检查文件格式。") from error
    else:
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ValueError("附件扩展名为PDF，但实际内容不是PDF。")
        from pdf_ocr import inspect_pdf, capabilities

        pages, needed = inspect_pdf(path)
        if needed and not capabilities()["available"]:
            raise ValueError(
                "扫描PDF识别组件尚未就绪，请先初始化鲸研环境，或提供DOCX/文字版PDF。"
            )
        return {
            "format": "pdf",
            "pages": len(pages),
            "requiresOCR": bool(needed),
            "ocrPages": [i + 1 for i in needed],
        }
