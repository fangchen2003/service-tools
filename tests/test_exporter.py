"""打包导出：zip 含图片 + prompts.txt；epub 是结构合法的电子书（mimetype 未压缩且在首位）。"""
import io
import zipfile

from app import exporter

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d49444154789c636060606000000005000157a6b5a30000000049454e44ae426082")
ROWS = [(1, 1_000_000.0, "image/png", "1girl, forest", "lowres",
         '{"params":{"seed":42,"sampler":"k_euler","steps":28,"scale":5,"width":832,"height":1216}}', PNG),
        (2, 1_000_100.0, "image/png", "1boy, city", "", "", PNG)]


def test_zip_has_images_and_prompts():
    data = exporter.build_zip(ROWS, "小明")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        assert sum(n.endswith(".png") for n in names) == 2 and "prompts.txt" in names
        txt = z.read("prompts.txt").decode()
        assert "1girl, forest" in txt and "种子 42" in txt


def test_epub_is_valid_structure():
    data = exporter.build_epub(ROWS, "小明")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        # EPUB 规定：第一个条目必须是未压缩的 mimetype
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        names = z.namelist()
        assert "META-INF/container.xml" in names and "OEBPS/content.opf" in names and "OEBPS/toc.ncx" in names
        assert sum(n.startswith("OEBPS/img") for n in names) == 2
        assert sum(n.startswith("OEBPS/p") and n.endswith(".xhtml") for n in names) == 2
        assert z.testzip() is None
