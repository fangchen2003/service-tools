"""把成员的原图打包成 ZIP 或 EPUB（电子书）。后台导出和用户端自助打包共用。

rows 来自 db.audit_images_for：(id, ts, image_type, prompt, negative, extra, image)。
纯标准库，不依赖外部包；在线程里跑（可能较大较慢）。
"""
from __future__ import annotations

import html
import io
import json
import time
import zipfile

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _params_line(extra: str) -> str:
    try:
        p = (json.loads(extra) or {}).get("params") or {}
    except (TypeError, ValueError):
        return ""
    bits = []
    if p.get("seed") is not None:
        bits.append(f"种子 {p['seed']}")
    if p.get("sampler"):
        bits.append(str(p["sampler"]))
    if p.get("steps"):
        bits.append(f"{p['steps']}步")
    if p.get("scale") is not None:
        bits.append(f"CFG {p['scale']}")
    if p.get("width") and p.get("height"):
        bits.append(f"{p['width']}×{p['height']}")
    return " · ".join(bits)


def build_zip(rows: list, who: str) -> bytes:
    """原图 zip + prompts.txt（每张的正面 / 负面 / 参数）。"""
    buf = io.BytesIO()
    lines = [f"{who} 的作品 · 共 {len(rows)} 张 · 导出于 {time.strftime('%Y-%m-%d %H:%M')}", ""]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i, r in enumerate(rows, 1):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(r[1]))
            fn = f"{i:04d}_{stamp}.{_EXT.get(r[2], 'png')}"
            z.writestr(fn, bytes(r[6]))
            params = _params_line(r[5])
            lines.append(f"[{fn}]\n正面：{r[3] or ''}\n负面：{r[4] or ''}" + (f"\n参数：{params}" if params else "") + "\n")
        z.writestr("prompts.txt", "\n".join(lines))
    return buf.getvalue()


def _page_xhtml(title: str, img_name: str, prompt: str, negative: str, params: str, when: str) -> str:
    e = html.escape
    neg = f'<p class="neg"><b>负面</b> {e(negative)}</p>' if negative else ""
    par = f'<p class="par">{e(params)}</p>' if params else ""
    return ("<?xml version='1.0' encoding='utf-8'?>\n"
            "<!DOCTYPE html>\n<html xmlns='http://www.w3.org/1999/xhtml'><head>"
            f"<title>{e(title)}</title><link rel='stylesheet' href='style.css' type='text/css'/></head>"
            f"<body><div class='pg'><img src='{e(img_name)}' alt=''/>"
            f"<p class='t'>{e(when)}</p><p class='p'><b>正面</b> {e(prompt)}</p>{neg}{par}</div></body></html>")


def build_epub(rows: list, who: str) -> bytes:
    """一张图一页的电子书：封面 + 每页图 + 提示词 / 参数。生成标准 EPUB2（zip 结构）。"""
    e = html.escape
    buf = io.BytesIO()
    now = time.strftime("%Y-%m-%d")
    title = f"{who} 的作品集"
    manifest, spine, nav_items, page_files = [], [], [], []
    for i, r in enumerate(rows, 1):
        ext = _EXT.get(r[2], "png")
        img_name = f"img{i:04d}.{ext}"
        page_name = f"p{i:04d}.xhtml"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r[1]))
        page_files.append((page_name, img_name, bytes(r[6]),
                           _page_xhtml(f"第 {i} 张", img_name, r[3] or "", r[4] or "", _params_line(r[5]), when)))
        media = "image/jpeg" if ext == "jpg" else f"image/{ext}"
        manifest.append(f"<item id='img{i}' href='{img_name}' media-type='{media}'/>")
        manifest.append(f"<item id='pg{i}' href='{page_name}' media-type='application/xhtml+xml'/>")
        spine.append(f"<itemref idref='pg{i}'/>")
        nav_items.append(f"<navPoint id='n{i}' playOrder='{i}'><navLabel><text>第 {i} 张</text></navLabel>"
                         f"<content src='{page_name}'/></navPoint>")
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<package xmlns='http://www.idpf.org/2007/opf' version='2.0' unique-identifier='bid'>"
           f"<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:title>{e(title)}</dc:title>"
           f"<dc:creator>{e(who)}</dc:creator><dc:language>zh</dc:language>"
           f"<dc:identifier id='bid'>owl-{int(time.time())}</dc:identifier><dc:date>{now}</dc:date></metadata>"
           "<manifest><item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>"
           "<item id='css' href='style.css' media-type='text/css'/>"
           "<item id='cover' href='cover.xhtml' media-type='application/xhtml+xml'/>"
           + "".join(manifest) + "</manifest>"
           "<spine toc='ncx'><itemref idref='cover'/>" + "".join(spine) + "</spine></package>")
    ncx = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/' version='2005-1'>"
           f"<head></head><docTitle><text>{e(title)}</text></docTitle><navMap>"
           f"<navPoint id='cover' playOrder='0'><navLabel><text>封面</text></navLabel><content src='cover.xhtml'/></navPoint>"
           + "".join(nav_items) + "</navMap></ncx>")
    cover = ("<?xml version='1.0' encoding='utf-8'?>\n<!DOCTYPE html>"
             "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>封面</title>"
             "<link rel='stylesheet' href='style.css' type='text/css'/></head>"
             f"<body><div class='cover'><h1>{e(title)}</h1><p>共 {len(rows)} 张 · 导出于 {now}</p>"
             "<p class='tip'>猫头鹰公益站</p></div></body></html>")
    css = ("body{margin:0;font-family:sans-serif}.pg{text-align:center;padding:12px}"
           ".pg img{max-width:100%;height:auto}.t{color:#888;font-size:.8em}"
           ".p,.neg,.par{text-align:left;font-size:.85em;line-height:1.5;word-break:break-all}"
           ".neg{color:#555}.par{color:#888;font-family:monospace;font-size:.78em}"
           ".cover{text-align:center;padding:30% 10%}.cover h1{font-size:1.6em}.tip{color:#aaa;margin-top:40px}")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   "<?xml version='1.0'?>\n<container version='1.0' xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                   "<rootfiles><rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/>"
                   "</rootfiles></container>")
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", css)
        z.writestr("OEBPS/cover.xhtml", cover)
        for page_name, img_name, img_bytes, xhtml in page_files:
            z.writestr("OEBPS/" + img_name, img_bytes)
            z.writestr("OEBPS/" + page_name, xhtml)
    return buf.getvalue()
