"""Bounded, offline parsing in a separate process. No external relationships are fetched."""
import csv
import io
import json
from pathlib import Path
import sys
import warnings
import zipfile
from defusedxml import ElementTree as ET

MAX_TEXT = 160000
MAX_PARTS = 5000
MAX_EXPANDED = 32 * 1024 * 1024


class ParseError(ValueError):
    pass


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ParseError('文档含不支持的 XML 声明，请另存为普通文档')
    return ET.fromstring(data)


def parse(path, suffix, preview):
    parts = []
    size = 0

    def add(location, text):
        nonlocal size
        text = text.strip()
        if not text:
            return
        size += len(text)
        if size > MAX_TEXT or len(parts) >= MAX_PARTS:
            raise ParseError('解析结果超过 16 万字或 5000 段预算，请拆分文档后上传')
        parts.append({'location': location, 'text': text})

    if suffix in ('.png', '.jpg', '.jpeg', '.webp'):
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 16000000
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                    raise ParseError('仅支持静态 PNG、JPEG、WebP 图片')
                image.load()
                image = ImageOps.exif_transpose(image).convert('RGB')
                width, height = image.size
                if width * height > 16000000:
                    raise ParseError('图片超过 1600 万像素，请缩小后上传')
                # Normalize pixels and remove EXIF/other metadata, without silently resizing.
                image.save(preview, format='JPEG', quality=90)
        if preview.stat().st_size > 20 * 1024 * 1024:
            raise ParseError('图片转换后超过预算，请压缩后上传')
        return {'kind': 'image', 'parts': [], 'width': width, 'height': height, 'notes': []}
    if suffix in ('.txt', '.md', '.csv'):
        data = path.read_bytes()
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = data.decode('gb18030')
            except UnicodeDecodeError:
                raise ParseError('无法识别文字编码，请转换为 UTF-8 后上传') from None
        if '\x00' in text:
            raise ParseError('文件不是可读取的文本')
        if suffix == '.csv':
            csv.field_size_limit(MAX_TEXT)
            for index, row in enumerate(csv.reader(io.StringIO(text)), 1):
                add(f'第 {index} 行', json.dumps(row, ensure_ascii=False))
        else:
            for index, line in enumerate(text.splitlines(), 1):
                add(f'第 {index} 行', line)
    elif suffix in ('.docx', '.xlsx'):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [v.filename for v in infos]
            if (len(infos) > 3000 or len(set(names)) != len(names) or
                    sum(v.file_size for v in infos) > MAX_EXPANDED or
                    any(v.flag_bits & 1 or v.file_size > 12 * 1024 * 1024 for v in infos)):
                raise ParseError('压缩文档超过解析预算或存在加密，请另存或拆分后上传')
            if suffix == '.docx':
                root = xml(archive.read('word/document.xml'))
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                for index, p in enumerate(root.findall('.//w:p', ns), 1):
                    add(f'第 {index} 段', ''.join(t.text or '' for t in p.findall('.//w:t', ns)))
            else:
                ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                strings = []
                if 'xl/sharedStrings.xml' in names:
                    for si in xml(archive.read('xl/sharedStrings.xml')).findall('s:si', ns):
                        strings.append(''.join(t.text or '' for t in si.findall('.//s:t', ns)))
                book = xml(archive.read('xl/workbook.xml'))
                sheets = book.findall('s:sheets/s:sheet', ns)
                rels = xml(archive.read('xl/_rels/workbook.xml.rels'))
                targets = {r.get('Id'): r.get('Target') for r in rels if r.get('TargetMode') != 'External'}
                for sheet in sheets:
                    rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                    target = targets.get(rid, '')
                    target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
                    if '..' in Path(target).parts or not target.startswith('xl/worksheets/'):
                        raise ParseError('工作表引用不受支持，请另存为 XLSX 后上传')
                    for cell in xml(archive.read(target)).findall('.//s:sheetData/s:row/s:c', ns):
                        if cell.find('s:f', ns) is not None:
                            raise ParseError('工作表包含公式，首批不计算或信任缓存值；请转换为数值后上传')
                        val = cell.find('s:v', ns)
                        text = val.text or '' if val is not None else ''
                        if cell.get('t') == 's':
                            text = strings[int(text)]
                        elif cell.get('t') == 'inlineStr':
                            text = ''.join(t.text or '' for t in cell.findall('.//s:t', ns))
                        add(f"工作表 {sheet.get('name', '')} · {cell.get('r', '?')}", text)
    elif suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or len(reader.pages) > 300:
            raise ParseError('PDF 加密或超过 300 页，请解密或拆分后上传')
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if not text.strip():
                raise ParseError(f'PDF 第 {index} 页没有可提取文本，可能需要 OCR；未声明已完整阅读')
            add(f'第 {index} 页', text)
    else:
        raise ParseError('此文件格式尚未支持')
    if not parts:
        raise ParseError('未提取到文本，请检查文件或转换格式')
    return {'kind': 'document', 'parts': parts, 'notes': ['文档仅提取正文或表格文字，不执行宏、公式或外部链接；版式和嵌入图片不参与理解。']}


def main():
    try:
        if sys.platform == 'linux':
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
            resource.setrlimit(resource.RLIMIT_FSIZE, (24 * 1024**2, 24 * 1024**2))
        path, suffix, preview, output = sys.argv[1:]
        result = parse(Path(path), suffix, Path(preview))
    except ParseError as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': '无法安全解析文件，请检查格式或另存后上传'}
    Path(sys.argv[4]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
