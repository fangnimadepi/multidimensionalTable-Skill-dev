"""Page-preserving local PDF text/OCR extraction; no remote inference or downloads."""

from __future__ import annotations
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from review_common import atomic_write_json, sha256
from llm_runtime import emit, check_cancelled

ROOT = Path(__file__).resolve().parent.parent
VERSION = "document-text-ocr-v2"
DPI = 170
MIN_NATIVE = 100
MODEL_NAMES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
}
_guard = threading.Lock()
_locks = {}
_pdfium_lock = (
    threading.RLock()
)  # PDFium is not thread safe, including across documents.
_engine_lock = threading.RLock()
_engine = None
_identity = {}


def backend_name():
    return os.environ.get(
        "PROJECT_ASSISTANT_OCR_ENGINE",
        (
            "rapidocr"
            if os.name == "nt"
            else "vision" if sys.platform == "darwin" else "rapidocr"
        ),
    )


def backend_identity():
    name = backend_name()
    if os.environ.get("PROJECT_ASSISTANT_TEXT_ONLY") == "1":
        raise ValueError(
            "本服务器尚未启用扫描PDF识别，请联系维护人员安装OCR组件，或提供可提取文字的PDF/DOCX。"
        )
    with _guard:
        if name in _identity:
            return copy.deepcopy(_identity[name])
        if name == "vision":
            if not shutil.which("swift") or not shutil.which("pdftoppm"):
                raise ValueError("本机缺少Vision OCR或PDF渲染工具。")
            value = {
                "backend": name,
                "engine_sha256": sha256(ROOT / "scripts/ocr_page.swift"),
                "dpi": DPI,
            }
        elif name == "rapidocr":
            for package in ("rapidocr", "onnxruntime", "pypdfium2"):
                if importlib.util.find_spec(package) is None:
                    raise ValueError(
                        "本机OCR组件尚未安装完整，请安装scripts/requirements-ocr.txt。"
                    )
            directory = (
                Path(importlib.util.find_spec("rapidocr").origin).parent / "models"
            )
            paths = {key: directory / name for key, name in MODEL_NAMES.items()}
            if not all(path.is_file() for path in paths.values()):
                raise ValueError(
                    "本机OCR模型缺少，请重新安装锁定版本的rapidocr；识别时不会自动联网下载。"
                )
            value = {
                "backend": name,
                "dpi": DPI,
                "model_sha256": {key: sha256(path) for key, path in paths.items()},
                "packages": {
                    p: importlib.metadata.version(p)
                    for p in ("rapidocr", "onnxruntime", "pypdfium2")
                },
                "threads": 2,
                "text_score": 0.5,
                "max_side_len": 2000,
            }
        else:
            raise ValueError("未知OCR后端，请使用rapidocr或vision。")
        _identity[name] = value
        return copy.deepcopy(value)


def capabilities():
    try:
        value = backend_identity()
        return {"available": True, "backend": value["backend"]}
    except (ValueError, OSError, ImportError) as error:
        return {"available": False, "backend": backend_name(), "reason": str(error)}


def rapid_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR

            directory = (
                Path(importlib.util.find_spec("rapidocr").origin).parent / "models"
            )
            _engine = RapidOCR(
                params={
                    **{
                        key + ".model_path": str(directory / name)
                        for key, name in MODEL_NAMES.items()
                    },
                    "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    "Global.log_level": "error",
                    "Global.max_side_len": 2000,
                    "Global.text_score": 0.5,
                }
            )
        return _engine


def recognize_image(image):
    """Return literal recognizer text, scores and boxes; never infer missing values."""
    with _engine_lock:
        result = rapid_engine()(image)
    if result.txts is None:
        return []
    return [
        {
            "text": str(text),
            "confidence": round(float(score), 6),
            "box": [[round(float(x), 3), round(float(y), 3)] for x, y in box],
        }
        for text, score, box in zip(result.txts, result.scores, result.boxes)
    ]


def render_page(path, index):
    import pypdfium2 as pdfium

    # Copy pixels before closing bitmap/document; no native resources escape the mutex.
    with _pdfium_lock:
        document = pdfium.PdfDocument(str(path))
        try:
            page = document[index]
            try:
                width, height = page.get_size()
                scale = min(
                    DPI / 72,
                    2400 / max(width, height),
                    (8_000_000 / max(1, width * height)) ** 0.5,
                )
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    try:
                        return image.convert("RGB").copy()
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()


def page_has_image(page, seen=None):
    seen = set() if seen is None else seen
    resources = page.get("/Resources") or {}
    for item in (resources.get("/XObject") or {}).values():
        marker = (getattr(item, "idnum", id(item)), getattr(item, "generation", 0))
        if marker in seen:
            continue
        seen.add(marker)
        value = item.get_object()
        if value.get("/Subtype") == "/Image":
            return True
        if value.get("/Subtype") == "/Form" and page_has_image(value, seen):
            return True
    return False


def inspect_pdf(path):
    try:
        reader = PdfReader(path)
    except (PdfReadError, ValueError) as error:
        raise ValueError("PDF内容无法读取，请检查文件是否完整。") from error
    if reader.is_encrypted:
        raise ValueError("请提供未加密的PDF总结报告。")
    if not 1 <= len(reader.pages) <= 200:
        raise ValueError("本轮支持1至200页的PDF总结报告。")
    pages = [page.extract_text() or "" for page in reader.pages]
    needed = [
        i
        for i, page in enumerate(reader.pages)
        if len(pages[i].strip()) < MIN_NATIVE and page_has_image(page)
    ]
    return pages, needed


def content_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def read_cache(path, identity):
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("identity") != identity or saved.get(
            "payload_sha256"
        ) != content_hash(saved["payload"]):
            return None
        data = saved["payload"]
        if (
            len(data["pages"]) != identity["page_count"]
            or len(data["results"]) != identity["page_count"]
            or any(not isinstance(p, str) for p in data["pages"])
            or [v["page"] for v in data["results"]]
            != list(range(1, identity["page_count"] + 1))
        ):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read(path, force_ocr=False):
    path = Path(path)
    native, needed = inspect_pdf(path)
    if force_ocr:
        needed = list(range(len(native)))
    if not needed:
        if not any(page.strip() for page in native):
            raise ValueError("该PDF没有可读取的文字或可识别的页面图像。")
        return {
            "pages": native,
            "results": [
                {"page": i + 1, "source": "native", "lines": []}
                for i in range(len(native))
            ],
            "ocr": {
                "scanned": False,
                "pages": len(native),
                "ocr_pages": [],
                "cache_hit": False,
            },
        }
    engine = backend_identity()
    identity = {
        "version": VERSION,
        "file_sha256": sha256(path),
        "engine": engine,
        "page_count": len(native),
        "ocr_pages": [i + 1 for i in needed],
    }
    key = content_hash(identity)
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    emit("ocr_wait", pages=len(needed), backend=engine["backend"])
    with lock:
        cache = ROOT / "runs/document-ocr-cache" / (key + ".json")
        saved = read_cache(cache, identity)
        if saved:
            saved["ocr"]["cache_hit"] = True
            emit("ocr_cache_hit", pages=len(needed), backend=engine["backend"])
            return saved
        started = time.monotonic()
        pages = list(native)
        results = [
            {"page": i + 1, "source": "native", "lines": []} for i in range(len(native))
        ]
        emit("ocr_start", pages=len(needed), backend=engine["backend"])
        vision = None
        if engine["backend"] == "vision":
            # Keep the existing macOS rendering/recognition path, including complete page order.
            with tempfile.TemporaryDirectory(prefix="project-vision-") as temporary:
                subprocess.run(
                    [
                        "pdftoppm",
                        "-r",
                        str(DPI),
                        "-png",
                        str(path),
                        str(Path(temporary) / "page"),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                images = sorted(
                    Path(temporary).glob("page-*.png"),
                    key=lambda p: int(p.stem.rsplit("-", 1)[1]),
                )
                run = subprocess.run(
                    ["swift", str(ROOT / "scripts/ocr_page.swift"), *map(str, images)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                vision = [
                    json.loads(line) for line in run.stdout.splitlines() if line.strip()
                ]
                if len(vision) != len(native):
                    raise ValueError("OCR页数与原PDF不一致，已停止。")
        partial_hits = 0
        for position, index in enumerate(needed, 1):
            check_cancelled()
            emit(
                "ocr_progress",
                page=position,
                pages=len(needed),
                backend=engine["backend"],
            )
            partial = cache.parent / key / (str(index + 1) + ".json")
            prior = read_cache(
                partial, {**identity, "page_count": 1, "page_index": index}
            )
            if prior:
                lines = prior["results"][0]["lines"]
                partial_hits += 1
            elif vision is not None:
                lines = vision[index]["lines"]
            else:
                image = render_page(path, index)
                try:
                    lines = recognize_image(image)
                finally:
                    image.close()
            text = "\n".join(line["text"] for line in lines)
            if len(text.strip()) >= len(native[index].strip()):
                pages[index] = text
            results[index] = {"page": index + 1, "source": "ocr", "lines": lines}
            # Partial OCR can resume after a stopped job. The one-page cache carries
            # its original index inside identity while its row numbering remains local.
            payload = {
                "pages": [pages[index]],
                "results": [{"page": 1, "source": "ocr", "lines": lines}],
            }
            atomic_write_json(
                partial,
                {
                    "identity": {**identity, "page_count": 1, "page_index": index},
                    "payload": payload,
                    "payload_sha256": content_hash(payload),
                },
            )
        if sum(len(value.strip()) for value in pages) < MIN_NATIVE:
            raise ValueError(
                "该PDF识别后仍未获得足够可用文字，请提供更清晰的扫描件或DOCX。"
            )
        scores = [
            float(line["confidence"]) for result in results for line in result["lines"]
        ]
        info = {
            "scanned": True,
            "pages": len(pages),
            "ocr_pages": [i + 1 for i in needed],
            "backend": engine["backend"],
            "engine_identity": engine,
            "mean_confidence": round(sum(scores) / len(scores), 3) if scores else None,
            "seconds": round(time.monotonic() - started, 3),
            "cache_hit": False,
            "page_cache_hits": partial_hits,
        }
        payload = {"pages": pages, "results": results, "ocr": info}
        atomic_write_json(
            cache,
            {
                "identity": identity,
                "payload": payload,
                "payload_sha256": content_hash(payload),
            },
        )
        emit(
            "ocr_end",
            pages=len(needed),
            backend=engine["backend"],
            durationSeconds=info["seconds"],
        )
        return payload


def read_pdf_pages(path):
    value = _read(path)
    return value["pages"], value["ocr"]


def ocr_document(path):
    backend_identity()  # Reject unavailable engines before invoking a platform tool.
    value = _read(path, force_ocr=True)
    return value["pages"], value["results"]


def self_check():
    """Local Chinese/numeric recognition, including actual model/DLL loading."""
    backend_identity()
    if backend_name() != "rapidocr":
        return {
            "backend": backend_name(),
            "available": True,
            "recognition_test": "not_requested",
        }
    from PIL import Image, ImageDraw, ImageFont
    candidates = [
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"),
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf"),
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    filename = next((name for name in candidates if Path(name).is_file()), None)
    if filename is None:
        return {"backend": "rapidocr", "available": True, "recognition_test": "not_run_no_test_font"}
    image = Image.new("RGB", (1000, 170), "white")
    try:
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(filename, 40)
        draw.text((25, 20), "项目中期目标 完成12项", font=font, fill="black")
        draw.text((25, 85), "2026年8月31日 OCR TEST", font=font, fill="black")
        try:
            lines = recognize_image(image)
        except ImportError as error:
            raise ValueError(
                "本机OCR运行组件无法加载；Windows请安装微软Visual C++ x64运行库，再重启服务自检。"
            ) from error
    finally:
        image.close()
    text = "".join(v["text"] for v in lines).replace(" ", "")
    if not all(value in text for value in ["项目", "12", "2026"]):
        raise ValueError("本机OCR中英文或数字自检未通过，尚不能识别扫描PDF。")
    return {"backend": "rapidocr", "available": True, "recognition_test": "passed"}
