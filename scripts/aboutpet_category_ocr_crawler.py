#!/usr/bin/env python3
"""AboutPet category crawler with OCR fallback for Korean ingredient fields."""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from paddleocr import PaddleOCR
except Exception:  # pragma: no cover - optional runtime dependency
    PaddleOCR = None

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

RAW_MATERIAL_KEYS = ["원재료", "원재료명", "원료", "원료명", "주원료"]
INGREDIENT_KEYS = [
    "성분",
    "성분량",
    "등록성분",
    "영양성분",
    "보증성분",
    "조성분",
    "조단백",
    "조지방",
    "조회분",
    "조섬유",
]


@dataclass
class ProductSeed:
    goods_id: str
    product_name: str
    list_image_url: str


@dataclass
class ProductResult:
    goods_id: str
    product_name: str
    product_image_url: str
    product_image_path: str
    edit_image_count: int
    edit_image_urls: str
    edit_image_paths: str
    ingredient: str
    ingredient_source: str
    raw_material: str
    raw_material_source: str
    detail_url: str


class OCRHelper:
    def __init__(self, enabled: bool, lang: str = "korean", use_gpu: bool = False, max_images: int = 6):
        self.enabled = enabled
        self.lang = lang
        self.use_gpu = use_gpu
        self.max_images = max_images
        self._ocr = None
        self._failed = False

    def _ensure_engine(self) -> bool:
        if not self.enabled:
            return False
        if self._failed:
            return False
        if self._ocr is not None:
            return True
        if PaddleOCR is None:
            logging.warning("PaddleOCR 미설치: OCR 단계를 건너뜁니다.")
            self._failed = True
            return False
        try:
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self.lang, use_gpu=self.use_gpu, show_log=False)
            return True
        except Exception as exc:  # pragma: no cover - runtime env dependent
            logging.warning("PaddleOCR 초기화 실패: %s", exc)
            self._failed = True
            return False

    @staticmethod
    def _flatten_ocr_text(result: object) -> List[str]:
        texts: List[str] = []
        stack: List[object] = [result]

        while stack:
            item = stack.pop()
            if item is None:
                continue

            if isinstance(item, dict):
                for key in ("text", "rec_text", "rec_texts"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        texts.append(val.strip())
                    elif isinstance(val, list):
                        for elem in val:
                            if isinstance(elem, str) and elem.strip():
                                texts.append(elem.strip())
                for val in item.values():
                    if isinstance(val, (dict, list, tuple)):
                        stack.append(val)
                continue

            if isinstance(item, (list, tuple)):
                if (
                    len(item) == 2
                    and isinstance(item[1], (list, tuple))
                    and len(item[1]) >= 1
                    and isinstance(item[1][0], str)
                ):
                    txt = item[1][0].strip()
                    if txt:
                        texts.append(txt)
                    continue
                stack.extend(item)

        return texts

    def extract_text(self, image_urls: Sequence[str], session: requests.Session, referer: str) -> str:
        if not self._ensure_engine():
            return ""

        unique_urls: List[str] = []
        seen = set()
        for url in image_urls:
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            unique_urls.append(url)
            if len(unique_urls) >= self.max_images:
                break

        texts: List[str] = []
        for img_url in unique_urls:
            try:
                resp = session.get(img_url, headers={"Referer": referer}, timeout=20)
                if resp.status_code != 200 or not resp.content:
                    continue

                suffix = ".jpg"
                ct = (resp.headers.get("content-type") or "").lower()
                if "png" in ct:
                    suffix = ".png"
                elif "webp" in ct:
                    suffix = ".webp"

                with tempfile.NamedTemporaryFile(delete=True, suffix=suffix) as tf:
                    tf.write(resp.content)
                    tf.flush()
                    ocr_result = self._ocr.ocr(tf.name, cls=True)

                texts.extend(self._flatten_ocr_text(ocr_result))
                time.sleep(0.05)
            except Exception:
                continue

        return "\n".join(texts)


def parse_category_url(category_url: str) -> Dict[str, str]:
    parsed = urlparse(category_url)
    query = parse_qs(parsed.query)

    required = ["cateCdL", "cateCdM", "dispClsfNo"]
    missing = [k for k in required if not query.get(k)]
    if missing:
        raise ValueError(f"카테고리 URL에 필수 파라미터 누락: {', '.join(missing)}")

    return {
        "cateCdL": query["cateCdL"][0],
        "cateCdM": query["cateCdM"][0],
        "dispClsfNo": query["dispClsfNo"][0],
    }


def clean_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    txt = html.unescape(raw)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def parse_goods_count(html_text: str) -> int:
    m = re.search(r"var\s+goodsCount\s*=\s*'?(\d+)'?", html_text)
    if not m:
        return 0
    return int(m.group(1))


def fetch_list_page(
    session: requests.Session,
    base_url: str,
    category_url: str,
    params: Dict[str, str],
    page: int,
    rows: int,
) -> Tuple[int, List[ProductSeed]]:
    endpoint = urljoin(base_url, "/shop/getScateGoodsList")
    data = {
        "dispClsfNo": params["dispClsfNo"],
        "cateCdL": params["cateCdL"],
        "cateCdM": params["cateCdM"],
        "filters": "",
        "bndNos": "",
        "order": "APET",
        "page": str(page),
        "rows": str(rows),
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": category_url,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    resp = session.post(endpoint, data=data, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    seeds: Dict[str, ProductSeed] = {}

    for card in soup.select(".gd-item[data-goodsid]"):
        goods_id = (card.get("data-goodsid") or "").strip()
        if not goods_id:
            continue

        product_name = (card.get("data-productname") or "").strip()
        if not product_name:
            title = card.select_one(".gd-body .tit")
            product_name = clean_text(title.get_text(" ")) if title else ""

        img = card.select_one("img.thumb-img")
        img_url = ""
        if img:
            img_url = (img.get("src") or img.get("data-src") or "").strip()
            if img_url:
                img_url = urljoin(base_url, img_url)

        if goods_id not in seeds:
            seeds[goods_id] = ProductSeed(
                goods_id=goods_id,
                product_name=product_name,
                list_image_url=img_url,
            )

    return parse_goods_count(resp.text), list(seeds.values())


def extract_table_pairs(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for tr in soup.select("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if not th or not td:
            continue
        label = clean_text(str(th))
        value = clean_text(str(td))
        if label and value:
            pairs.append((label, value))
    return pairs


def pick_field_from_pairs(pairs: Sequence[Tuple[str, str]]) -> Tuple[str, str, str, str]:
    ingredient = ""
    ingredient_source = ""
    raw_material = ""
    raw_source = ""

    for label, value in pairs:
        if not raw_material and any(k in label for k in RAW_MATERIAL_KEYS):
            raw_material = value
            raw_source = f"table:{label}"
            continue
        if not ingredient and any(k in label for k in INGREDIENT_KEYS):
            ingredient = value
            ingredient_source = f"table:{label}"

    return ingredient, ingredient_source, raw_material, raw_source


def extract_from_text_blob(text: str) -> Tuple[str, str]:
    src = text.replace("\r", "\n")
    src = re.sub(r"\n{2,}", "\n", src)

    raw_patterns = [
        r"(?:원재료명?|원료명?|주원료)\s*[:：]\s*([^\n]{4,500})",
        r"(?:원재료명?|원료명?|주원료)\s*([^\n]{4,500})",
    ]
    ingredient_patterns = [
        r"(?:등록성분|영양성분|보증성분|조성분|성분량|성분)\s*[:：]\s*([^\n]{4,500})",
        r"(?:조단백|조지방|조섬유|조회분)\s*[:：]?\s*([^\n]{2,100})",
    ]

    raw_material = ""
    ingredient = ""

    for pat in raw_patterns:
        m = re.search(pat, src, flags=re.IGNORECASE)
        if m:
            raw_material = clean_text(m.group(1))
            break

    for pat in ingredient_patterns:
        m = re.search(pat, src, flags=re.IGNORECASE)
        if m:
            ingredient = clean_text(m.group(1))
            break

    return ingredient, raw_material


def parse_goods_desc_json(json_obj: Dict[str, object], base_url: str) -> Tuple[str, List[str], str]:
    goods_desc = json_obj.get("goodsDesc") if isinstance(json_obj, dict) else None
    if not isinstance(goods_desc, dict):
        return "", [], ""

    content_pc = goods_desc.get("contentPc")
    content_mobile = goods_desc.get("contentMobile")
    html_text = "\n".join([x for x in [content_pc, content_mobile] if isinstance(x, str) and x.strip()])

    if not html_text:
        return "", [], ""

    soup = BeautifulSoup(html_text, "html.parser")
    image_urls: List[str] = []
    for img in soup.select("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if src:
            image_urls.append(urljoin(base_url, src))

    text_blob = html.unescape(soup.get_text("\n", strip=True))
    text_blob = text_blob.replace("\xa0", " ")
    text_blob = re.sub(r"[ \t]+", " ", text_blob)
    text_blob = re.sub(r"\n{2,}", "\n", text_blob).strip()
    return html_text, image_urls, text_blob


def choose_preferred_text(*values: str) -> str:
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return ""


def dedupe_keep_order(values: Sequence[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        v = (value or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        result.append(v)
    return result


def download_main_image(session: requests.Session, image_url: str, save_dir: Path, goods_id: str) -> str:
    if not image_url:
        return ""

    try:
        resp = session.get(image_url, timeout=20)
        if resp.status_code != 200 or not resp.content:
            return ""

        ext = ".jpg"
        ct = (resp.headers.get("content-type") or "").lower()
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"

        save_dir.mkdir(parents=True, exist_ok=True)
        out_path = save_dir / f"{goods_id}{ext}"
        out_path.write_bytes(resp.content)
        return str(out_path)
    except Exception:
        return ""


def download_edit_images(
    session: requests.Session,
    image_urls: Sequence[str],
    save_dir: Path,
    goods_id: str,
    referer: str,
) -> List[str]:
    save_dir.mkdir(parents=True, exist_ok=True)
    goods_dir = save_dir / goods_id
    goods_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[str] = []
    urls = dedupe_keep_order(image_urls)

    for idx, img_url in enumerate(urls, start=1):
        try:
            resp = session.get(img_url, headers={"Referer": referer}, timeout=20)
            if resp.status_code != 200 or not resp.content:
                continue

            ext = ".jpg"
            ct = (resp.headers.get("content-type") or "").lower()
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            elif "gif" in ct:
                ext = ".gif"

            out_path = goods_dir / f"{idx:03d}{ext}"
            out_path.write_bytes(resp.content)
            saved_paths.append(str(out_path))
        except Exception:
            continue

    return saved_paths


def crawl_product(
    session: requests.Session,
    base_url: str,
    seed: ProductSeed,
    ocr_helper: OCRHelper,
    download_images_dir: Optional[Path],
) -> ProductResult:
    goods_id = seed.goods_id
    detail_url = urljoin(base_url, f"/goods/indexGoodsDetail?goodsId={goods_id}")

    product_name = seed.product_name
    main_image_url = seed.list_image_url
    ingredient = ""
    ingredient_source = ""
    raw_material = ""
    raw_source = ""

    detail_headers = {"Referer": detail_url, "X-Requested-With": "XMLHttpRequest"}

    desc_html_text = ""
    desc_text_blob = ""
    desc_images: List[str] = []
    edit_image_paths: List[str] = []

    try:
        index_resp = session.get(detail_url, timeout=20)
        if index_resp.ok:
            soup = BeautifulSoup(index_resp.text, "html.parser")
            title_node = soup.select_one(".pdInfos .names")
            if title_node:
                product_name = choose_preferred_text(title_node.get_text(" "), product_name)

            og = soup.select_one('meta[property="og:image"]')
            if og and og.get("content"):
                main_image_url = urljoin(base_url, og["content"].strip())
    except Exception:
        pass

    try:
        goods_detail_resp = session.get(
            urljoin(base_url, f"/goods/getGoodsDetail?goodsId={goods_id}"),
            headers=detail_headers,
            timeout=20,
        )
        if goods_detail_resp.ok:
            soup = BeautifulSoup(goods_detail_resp.text, "html.parser")
            pairs = extract_table_pairs(soup)
            ing, ing_src, raw, raw_src = pick_field_from_pairs(pairs)
            ingredient = choose_preferred_text(ingredient, ing)
            raw_material = choose_preferred_text(raw_material, raw)
            ingredient_source = ingredient_source or ing_src
            raw_source = raw_source or raw_src

            if not ingredient or not raw_material:
                table_text = clean_text(soup.get_text("\n", strip=True))
                ing2, raw2 = extract_from_text_blob(table_text)
                if not ingredient and ing2:
                    ingredient = ing2
                    ingredient_source = ingredient_source or "detail_text"
                if not raw_material and raw2:
                    raw_material = raw2
                    raw_source = raw_source or "detail_text"
    except Exception:
        pass

    try:
        desc_resp = session.post(
            urljoin(base_url, "/goods/getGoodsDesc"),
            data={"goodsId": goods_id},
            headers=detail_headers,
            timeout=20,
        )
        if desc_resp.ok:
            payload = desc_resp.json()
            desc_html_text, desc_images, desc_text_blob = parse_goods_desc_json(payload, base_url)
            desc_images = dedupe_keep_order(desc_images)
            ing3, raw3 = extract_from_text_blob(desc_text_blob)
            if not ingredient and ing3:
                ingredient = ing3
                ingredient_source = ingredient_source or "desc_text"
            if not raw_material and raw3:
                raw_material = raw3
                raw_source = raw_source or "desc_text"

            if not main_image_url and desc_images:
                main_image_url = desc_images[0]
    except Exception:
        pass

    if (not ingredient or not raw_material) and desc_images:
        ocr_text = ocr_helper.extract_text(desc_images, session, referer=detail_url)
        if ocr_text:
            ing4, raw4 = extract_from_text_blob(ocr_text)
            if not ingredient and ing4:
                ingredient = ing4
                ingredient_source = ingredient_source or "ocr"
            if not raw_material and raw4:
                raw_material = raw4
                raw_source = raw_source or "ocr"

    product_image_path = ""
    if download_images_dir and main_image_url:
        product_image_path = download_main_image(session, main_image_url, download_images_dir, goods_id)
    if download_images_dir and desc_images:
        edit_image_paths = download_edit_images(
            session=session,
            image_urls=desc_images,
            save_dir=download_images_dir / "editor_images",
            goods_id=goods_id,
            referer=detail_url,
        )

    return ProductResult(
        goods_id=goods_id,
        product_name=product_name,
        product_image_url=main_image_url,
        product_image_path=product_image_path,
        edit_image_count=len(desc_images),
        edit_image_urls=" | ".join(desc_images),
        edit_image_paths=" | ".join(edit_image_paths),
        ingredient=ingredient,
        ingredient_source=ingredient_source or "",
        raw_material=raw_material,
        raw_material_source=raw_source or "",
        detail_url=detail_url,
    )


def write_csv(path: Path, rows: Sequence[ProductResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "goods_id",
                "product_name",
                "product_image_url",
                "product_image_path",
                "edit_image_count",
                "edit_image_urls",
                "edit_image_paths",
                "ingredient",
                "ingredient_source",
                "raw_material",
                "raw_material_source",
                "detail_url",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def write_json(path: Path, rows: Sequence[ProductResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [r.__dict__ for r in rows]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)
    return sess


def run(args: argparse.Namespace) -> None:
    category_url = args.category_url.strip()
    base_url = f"{urlparse(category_url).scheme}://{urlparse(category_url).netloc}"
    cat_params = parse_category_url(category_url)

    session = build_session()
    ocr_helper = OCRHelper(
        enabled=args.ocr,
        lang=args.ocr_lang,
        use_gpu=args.ocr_gpu,
        max_images=args.ocr_max_images,
    )

    goods_count, first_page_items = fetch_list_page(
        session=session,
        base_url=base_url,
        category_url=category_url,
        params=cat_params,
        page=1,
        rows=args.rows,
    )

    total_pages = max(1, math.ceil(goods_count / args.rows)) if goods_count else 1
    if args.max_pages > 0:
        total_pages = min(total_pages, args.max_pages)

    all_seeds: Dict[str, ProductSeed] = {item.goods_id: item for item in first_page_items}
    logging.info("상품 목록 수집 시작: goods_count=%s, pages=%s", goods_count, total_pages)

    for page in range(2, total_pages + 1):
        _, page_items = fetch_list_page(
            session=session,
            base_url=base_url,
            category_url=category_url,
            params=cat_params,
            page=page,
            rows=args.rows,
        )
        for item in page_items:
            all_seeds.setdefault(item.goods_id, item)
        if args.sleep > 0:
            time.sleep(args.sleep)

    seeds = list(all_seeds.values())
    if args.max_products > 0:
        seeds = seeds[: args.max_products]
    logging.info("중복 제거 후 상품 수: %s", len(seeds))

    download_dir = Path(args.download_images_dir).resolve() if args.download_images_dir else None

    results: List[ProductResult] = []
    for idx, seed in enumerate(seeds, start=1):
        result = crawl_product(
            session=session,
            base_url=base_url,
            seed=seed,
            ocr_helper=ocr_helper,
            download_images_dir=download_dir,
        )
        results.append(result)

        if idx % 20 == 0 or idx == len(seeds):
            logging.info("진행률: %s/%s", idx, len(seeds))

        if args.sleep > 0:
            time.sleep(args.sleep)

    out_csv = Path(args.output_csv).resolve()
    out_json = Path(args.output_json).resolve() if args.output_json else None

    write_csv(out_csv, results)
    if out_json:
        write_json(out_json, results)

    missing_ingredient = sum(1 for r in results if not r.ingredient)
    missing_raw = sum(1 for r in results if not r.raw_material)

    logging.info("CSV 저장 완료: %s", out_csv)
    if out_json:
        logging.info("JSON 저장 완료: %s", out_json)
    logging.info("누락 현황 - 성분: %s, 원료명: %s", missing_ingredient, missing_raw)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AboutPet 카테고리 상품명/사진/성분/원료명 수집 크롤러 (OCR fallback 포함)",
    )
    parser.add_argument("category_url", help="어바웃펫 카테고리 URL")
    parser.add_argument("--rows", type=int, default=20, help="목록 API 페이지당 상품 수")
    parser.add_argument("--max-pages", type=int, default=0, help="테스트용 최대 페이지 수(0=전체)")
    parser.add_argument("--max-products", type=int, default=0, help="수집할 최대 상품 수(0=전체)")
    parser.add_argument("--sleep", type=float, default=0.12, help="요청 간 슬립(초)")

    parser.add_argument("--output-csv", default="output/aboutpet_products.csv", help="CSV 출력 파일 경로")
    parser.add_argument("--output-json", default="output/aboutpet_products.json", help="JSON 출력 파일 경로")

    parser.add_argument("--download-images-dir", default="output/images", help="이미지 저장 디렉터리(대표+에디터)")

    parser.add_argument("--ocr", dest="ocr", action="store_true", default=True, help="상세 이미지 OCR 활성화 (기본값: 켜짐)")
    parser.add_argument("--no-ocr", dest="ocr", action="store_false", help="상세 이미지 OCR 비활성화")
    parser.add_argument("--ocr-lang", default="korean", help="PaddleOCR 언어 코드")
    parser.add_argument("--ocr-gpu", action="store_true", help="PaddleOCR GPU 사용")
    parser.add_argument("--ocr-max-images", type=int, default=6, help="상품당 OCR 대상 이미지 최대 개수")

    parser.add_argument("--log-level", default="INFO", help="로그 레벨 (DEBUG/INFO/WARNING/ERROR)")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )

    run(args)


if __name__ == "__main__":
    main()
