#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为知笔记 MD -> 飞书云文档迁移工具

目录结构与 output/ 完全对应，统一放在飞书网盘「为知笔记」文件夹下:
  output/Terminal/Java/xxx.md  ->  为知笔记/Terminal/Java/xxx
  output/Work/yyy.md           ->  为知笔记/Work/yyy

单文件测试(放到「为知笔记」根目录下):
    python migrate_to_lark.py --file "...\\RabbitMQ.md"

批量迁移整个 output 目录(自动建目录树):
    python migrate_to_lark.py --all
"""

import re
import sys
import time
import threading
import webbrowser
import argparse
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import requests
except ImportError:
    print("缺少依赖，请执行: pip install requests")
    sys.exit(1)

# ---- 配置 ----------------------------------------------------------
APP_ID     = "cli_aaa84525xxxxxxxxxxxxxxx"
APP_SECRET = "hDfPwDBHdvxAxxxxxxxxxxxxxxxxxxxx"
OUTPUT_DIR = Path(r"C:\Users\Toper\Desktop\Wiznotes_tools-master\export_wiznotes\output")
ROOT_NAME  = "为知笔记"   # 飞书网盘顶层文件夹名称
BASE       = "https://open.feishu.cn/open-apis"

# ---- Token 缓存 -------------------------------------------------------
_TOK = {"v": None, "exp": 0.0, "type": "user"}  # type: user | tenant


def _get_tenant_token() -> str:
    """App 身份 Token（仅用于开发调试，正常不使用）"""
    r = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"Tenant token 获取失败: {d}")
    return d["tenant_access_token"]


def _get_user_token() -> str:
    """
    通过 OAuth 获取 user_access_token。
    本地启动一个回调服务器（端口 9988），然后弹出浏览器让用户授权。
    """
    # 如果已有有效的 user token，直接返回
    if _TOK["v"] and _TOK["type"] == "user" and time.time() < _TOK["exp"] - 60:
        return _TOK["v"]

    REDIRECT_URI   = "http://localhost:9988/callback"
    SCOPE          = "drive:drive docx:document"
    auth_code_box  = []

    # 回调服务器
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            if code:
                auth_code_box.append(code)
                body = b"<h2>\xe6\x8e\x88\xe6\x9d\x83\xe6\x88\x90\xe5\x8a\x9f\xef\xbc\x81\
\xe5\x8f\xaf\xe4\xbb\xa5\xe5\x85\xb3\xe9\x97\xad\xe6\xad\xa4\xe7\xaa\x97\xe5\x8f\xa3\xe4\xba\x86\xe3\x80\x82</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(400)
                self.end_headers()

    server = HTTPServer(("localhost", 9988), Handler)
    t = threading.Thread(target=server.handle_request)
    t.start()

    # 构建授权 URL
    params = {
        "app_id":       APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type":"code",
        "scope":        SCOPE,
    }
    auth_url = f"https://open.feishu.cn/open-apis/authen/v1/authorize?{urlencode(params)}"
    print(f"\n正在打开浏览器进行飞书授权，请登录你自己的飞书账号并点击同意...")
    webbrowser.open(auth_url)

    t.join(timeout=120)
    server.server_close()

    if not auth_code_box:
        raise RuntimeError("授权超时，未获到 code")

    # 用 code 换取 user_access_token
    r = requests.post(
        f"{BASE}/authen/v1/oidc/access_token",
        headers={"Authorization": f"Bearer {_get_tenant_token()}",
                 "Content-Type":  "application/json"},
        json={"grant_type": "authorization_code", "code": auth_code_box[0]}
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"user_access_token 获取失败: {d}")
    data = d["data"]
    _TOK["v"]    = data["access_token"]
    _TOK["exp"]  = time.time() + data.get("expires_in", 7200)
    _TOK["type"] = "user"
    print("\n授权成功！正在以你的身份进行迁移...将文档属为你自己。")
    return _TOK["v"]


def get_token() -> str:
    return _get_user_token()


def hdr() -> dict:
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

# ---- 飞书网盘文件夹 API -----------------------------------------------

def get_root_folder_token() -> str:
    r = requests.get(f"{BASE}/drive/explorer/v2/root_folder/meta", headers=hdr())
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"获取根目录失败: {d}")
    return d["data"]["token"]


def create_folder(name: str, parent_token: str) -> str:
    r = requests.post(
        f"{BASE}/drive/v1/files/create_folder",
        headers=hdr(),
        json={"name": name, "folder_token": parent_token}
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"创建文件夹「{name}」失败: {d}")
    return d["data"]["token"]


def ensure_folder_path(parts: list, wiz_root: str, cache: dict) -> str:
    """递归确保多层文件夹存在，返回最终层 token"""
    if not parts:
        return wiz_root
    key = tuple(parts)
    if key in cache:
        return cache[key]
    parent = ensure_folder_path(parts[:-1], wiz_root, cache)
    token  = create_folder(parts[-1], parent)
    cache[key] = token
    time.sleep(0.2)
    return token


# ---- 飞书文档 API -----------------------------------------------

def create_doc(title: str, folder_token: str = None) -> str:
    body = {"title": title}
    if folder_token:
        body["folder_token"] = folder_token
    r = requests.post(f"{BASE}/docx/v1/documents", headers=hdr(), json=body)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"创建文档失败: {d}")
    return d["data"]["document"]["document_id"]


def upload_image(doc_id: str, img_path: Path, index: int = -1):
    """
    上传图片并正确绑定到 image block：
      1. 创建空 image block（在指定 index 处）
      2. 上传图片取得 file_token（parent_node = image_block_id）
      3. PATCH image block，将 file_token 写入
    返回 True 成功，False 失败。
    """
    if not img_path.exists():
        print(f"    [警告] 图片不存在: {img_path.name}")
        return False

    # 1. 创建空 image block
    r1 = requests.post(
        f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
        headers=hdr(),
        json={"children": [{"block_type": 27, "image": {"align": 1}}], "index": index}
    )
    d1 = r1.json()
    if d1.get("code") != 0:
        print(f"    [警告] 创建 image block 失败: {d1.get('msg')}")
        return False
    image_block_id = d1["data"]["children"][0]["block_id"]

    # 2. 上传图片，parent_node 指向 image block
    raw = img_path.read_bytes()
    r2 = requests.post(
        f"{BASE}/drive/v1/medias/upload_all",
        headers={"Authorization": f"Bearer {get_token()}"},
        data={
            "file_name":   img_path.name,
            "parent_type": "docx_image",
            "parent_node": image_block_id,
            "size":        str(len(raw)),
        },
        files={"file": (img_path.name, raw)},
    )
    d2 = r2.json()
    if d2.get("code") != 0:
        print(f"    [警告] 图片上传失败: {img_path.name} -> {d2.get('msg')}")
        return False
    file_token = d2["data"]["file_token"]

    # 3. PATCH image block，把 file_token 写进去
    r3 = requests.patch(
        f"{BASE}/docx/v1/documents/{doc_id}/blocks/{image_block_id}",
        headers=hdr(),
        params={"document_revision_id": -1},
        json={"replace_image": {"token": file_token}}
    )
    d3 = r3.json()
    if d3.get("code") != 0:
        print(f"    [警告] image block token 写入失败: {d3.get('msg')}")
        return False
    return True


# 用于在 blocks 列表中占位的图片标记
class ImageBlock:
    def __init__(self, img_path: Path):
        self.img_path = img_path
        self.block_type = 27  # 为了已已兼容 push_blocks 里的日志


def get_child_count(doc_id: str) -> int:
    r = requests.get(
        f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}",
        headers=hdr()
    )
    d = r.json()
    if d.get("code") != 0:
        return 0
    return len(d["data"]["block"].get("children", []))


def push_blocks(doc_id: str, blocks: list):
    """push blocks，其中 ImageBlock 类型的用两步法写入"""
    if not blocks:
        return
    idx = get_child_count(doc_id)
    for i, b in enumerate(blocks):
        if isinstance(b, ImageBlock):
            ok = upload_image(doc_id, b.img_path, index=idx)
            if ok:
                print(f"    [图片] {b.img_path.name}")
                idx += 1
            else:
                r = requests.post(
                    f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                    headers=hdr(),
                    json={"children": [mk_paragraph(f"[图片上传失败: {b.img_path.name}]")], "index": idx},
                )
                if r.json().get("code") == 0:
                    idx += 1
        else:
            r = requests.post(
                f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                headers=hdr(),
                json={"children": [b], "index": idx},
            )
            d = r.json()
            if d.get("code") != 0:
                import json as _json
                print(f"  [警告] block[{i}] type={b.get('block_type')} 失败: {d.get('msg')}")
                print(f"    内容: {_json.dumps(b, ensure_ascii=False)[:200]}")
            else:
                idx += 1
        time.sleep(0.15)

# ---- Markdown 解析 -------------------------------------------------

def _style(bold=False, inline_code=False):
    return {"bold": bold, "inline_code": inline_code,
            "italic": False, "strikethrough": False, "underline": False}

def inline_elems(text: str) -> list:
    elems, last = [], 0
    for m in re.finditer(r'\*\*(.+?)\*\*|`([^`]+)`', text):
        if m.start() > last:
            elems.append({"text_run": {"content": text[last:m.start()], "text_element_style": _style()}})
        if m.group(1) is not None:
            elems.append({"text_run": {"content": m.group(1), "text_element_style": _style(bold=True)}})
        else:
            elems.append({"text_run": {"content": m.group(2), "text_element_style": _style(inline_code=True)}})
        last = m.end()
    if last < len(text):
        elems.append({"text_run": {"content": text[last:], "text_element_style": _style()}})
    if not elems:
        elems.append({"text_run": {"content": text, "text_element_style": _style()}})
    return elems


# 飞书 block_type 对应表
# MD # -> heading1(block_type=3), ## -> heading2(block_type=4), ### -> heading3(block_type=5) ...
HEADING_BT  = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8}
HEADING_KEY = {1: "heading1", 2: "heading2", 3: "heading3",
               4: "heading4", 5: "heading5", 6: "heading6"}

def mk_paragraph(text: str) -> dict:
    return {"block_type": 2, "text": {"elements": inline_elems(text), "style": {"align": 1}}}

def mk_heading(text: str, level: int) -> dict:
    bt  = HEADING_BT.get(level, 4)
    key = HEADING_KEY.get(level, "heading2")
    return {"block_type": bt, key: {"elements": inline_elems(text), "style": {"align": 1}}}

def mk_code(text: str) -> dict:
    # block_type 14 = Code Block, block_type 22 = Divider
    return {"block_type": 14, "code": {
        "elements": [{"text_run": {"content": text, "text_element_style": _style()}}],
        "style": {"language": 1, "wrap": False}
    }}

def mk_bullet(text: str) -> dict:
    return {"block_type": 12, "bullet": {"elements": inline_elems(text), "style": {"align": 1, "folded": False}}}

def mk_ordered(text: str) -> dict:
    return {"block_type": 13, "ordered": {"elements": inline_elems(text), "style": {"align": 1, "folded": False}}}

def mk_image(file_token: str) -> dict:
    return {"block_type": 27, "image": {"token": file_token, "align": 1}}


def md_to_blocks(body: str, doc_id: str, md_path: Path) -> list:
    blocks = []
    lines  = body.splitlines()
    md_dir = md_path.parent
    i = 0
    while i < len(lines):
        line = lines[i]
        # 代码块
        if line.startswith("```"):
            code_lines, i = [], i + 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append(mk_code("\n".join(code_lines)))
            i += 1
            continue
        # 图片：用 search 匹配行内任意位置（包括 > - ![]() 这种情况）
        img_m = re.search(r'!\[.*?\]\((.+?)\)', line)
        if img_m:
            # 图片前的文字单独成段
            prefix = line[:img_m.start()].strip().lstrip('> ').lstrip('- ').strip()
            if prefix:
                blocks.append(mk_paragraph(prefix))
            # 用 ImageBlock 占位，保持顺序
            blocks.append(ImageBlock(md_dir / img_m.group(1)))
            i += 1
            continue
        # 标题
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            blocks.append(mk_heading(m.group(2), len(m.group(1))))
            i += 1
            continue
        # 有序列表
        m = re.match(r'^\d+\.\s+(.*)', line)
        if m:
            blocks.append(mk_ordered(m.group(1)))
            i += 1
            continue
        # 无序列表
        m = re.match(r'^[-*+]\s+(.*)', line)
        if m:
            blocks.append(mk_bullet(m.group(1)))
            i += 1
            continue
        # 空行
        if not line.strip():
            i += 1
            continue
        # 其他（包括 > 引用块）：将 > 前缀尽可能山素划华，再按内容类型处理
        cleaned = re.sub(r'^(\s*>\s*)+', '', line).strip()
        if not cleaned:
            i += 1
            continue
        # 引用块内的图片
        img_m2 = re.search(r'!\[.*?\]\((.+?)\)', cleaned)
        if img_m2:
            prefix2 = cleaned[:img_m2.start()].strip().lstrip('- ').strip()
            if prefix2:
                blocks.append(mk_paragraph(prefix2))
            blocks.append(ImageBlock(md_dir / img_m2.group(1)))
            i += 1
            continue
        # 引用块内的有序列表
        m2 = re.match(r'^\d+\.\s+(.*)', cleaned)
        if m2:
            blocks.append(mk_ordered(m2.group(1)))
            i += 1
            continue
        # 引用块内的无序列表
        m2 = re.match(r'^[-*+]\s+(.*)', cleaned)
        if m2:
            blocks.append(mk_bullet(m2.group(1)))
            i += 1
            continue
        # 引用块内的普通文字
        blocks.append(mk_paragraph(cleaned))
        i += 1
    return blocks


def strip_frontmatter(content: str):
    meta = {}
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for ln in parts[1].splitlines():
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    meta[k.strip()] = v.strip()
            return meta, parts[2].lstrip("\n")
    return meta, content


# ---- 单文件迁移 ---------------------------------------------------

def migrate_one(md_path: Path, folder_token: str = None) -> str:
    print(f"\n{'='*60}")
    print(f"文件: {md_path.name}")

    content    = md_path.read_text(encoding="utf-8", errors="replace")
    meta, body = strip_frontmatter(content)
    raw_title  = meta.get("title", md_path.stem)
    title      = raw_title[:-3] if raw_title.endswith(".md") else raw_title
    print(f"标题: {title}")

    doc_id = create_doc(title, folder_token)
    print(f"文档 ID: {doc_id}")

    blocks = md_to_blocks(body, doc_id, md_path)
    print(f"Blocks: {len(blocks)}")

    push_blocks(doc_id, blocks)
    print(f"完成 -> https://feishu.cn/docx/{doc_id}")
    return doc_id


# ---- 批量迁移(自动建目录树) ---------------------------------------

def migrate_all():
    print("获取飞书网盘根目录...")
    root_token = get_root_folder_token()
    print(f"根目录 token: {root_token}")

    print(f"\n创建根文件夹「{ROOT_NAME}」...")
    wiz_root = create_folder(ROOT_NAME, root_token)
    print(f"「{ROOT_NAME}」 token: {wiz_root}")

    md_files     = sorted(OUTPUT_DIR.rglob("*.md"))
    total        = len(md_files)
    folder_cache = {}   # tuple(parts) -> folder_token
    ok, fail     = 0, []

    print(f"\n共找到 {total} 个 MD 文件，开始迁移...\n")

    for idx, md in enumerate(md_files, 1):
        try:
            rel   = md.relative_to(OUTPUT_DIR)
            parts = list(rel.parts[:-1])   # 去掉文件名

            if parts:
                folder_token = ensure_folder_path(parts, wiz_root, folder_cache)
            else:
                folder_token = wiz_root

            print(f"[{idx}/{total}] {'/'.join(parts) or '.'}/ ", end="")
            migrate_one(md, folder_token)
            ok += 1
        except Exception as e:
            print(f"  [失败] {md.name}: {e}")
            fail.append(str(md))
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"迁移完成: 成功 {ok} 个，失败 {len(fail)} 个")
    if fail:
        print("失败列表:")
        for f in fail:
            print(f"  {f}")


# ---- 入口 ----------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="为知笔记 -> 飞书云文档迁移工具")
    ap.add_argument("--file", help="单个 MD 文件路径(测试用)")
    ap.add_argument("--all",  action="store_true", help="批量迁移 output 目录下所有 MD")
    args = ap.parse_args()

    if args.file:
        print("获取飞书网盘根目录...")
        root_token = get_root_folder_token()
        print(f"创建根文件夹「{ROOT_NAME}」...")
        wiz_root = create_folder(ROOT_NAME, root_token)
        migrate_one(Path(args.file), wiz_root)
    elif args.all:
        migrate_all()
    else:
        test_file = OUTPUT_DIR / "Terminal" / "RabbitMQ.md"
        print("用法:")
        print(f'  python migrate_to_lark.py --file "{test_file}"')
        print( '  python migrate_to_lark.py --all')
