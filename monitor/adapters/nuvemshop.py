"""Nuvemshop (Tiendanube).

O que a inspeção das lojas mostrou:
- O sitemap.xml é gerado com atraso (produtos novos demoram a aparecer e removidos
  continuam lá), então NÃO usamos o sitemap.
- A listagem /produtos/page/N/?results_only=true traz, para cada produto, um JSON-LD
  (nome, URL, imagem, preço, availability, inventoryLevel) e o atributo
  data-store="stock-product-{id}-{estoque}" ("infinite" = sem controle de estoque).
  Produtos esgotados também aparecem. Ou seja: o catálogo inteiro, com estoque,
  em ~1 requisição a cada 20 produtos.
- Estoque por variante só existe na página do produto, no atributo data-variants (JSON).
"""
from __future__ import annotations

import html
import json
import re
from typing import Dict, List, Optional, Tuple

from ..fetcher import FetchError, is_password_page
from ..models import Product, ScanResult, Variant, clean_text
from .base import Adapter, AdapterError

MAX_PAGES = 300

ITEM_RE = re.compile(r'data-product-id="(\d+)"')
LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*structured-data\.item[^>]*>(.*?)</script>", re.S)
STOCK_RE = re.compile(r'data-store="stock-product-(\d+)-([^"]*)"')
VARIANTS_RE = re.compile(r'data-variants="([^"]*)"')
IMG_SIZE_RE = re.compile(r"-\d+-\d+\.(?:webp|jpe?g|png)$", re.I)

AVAILABLE_MARKERS = ("InStock", "LimitedAvailability", "PreOrder", "PreSale", "OnlineOnly", "BackOrder")


class NuvemshopAdapter(Adapter):
    name = "nuvemshop"
    variants_in_listing = False

    @staticmethod
    def detect(resp) -> bool:
        text = resp.text.lower()
        return "mitiendanube.com" in text or "nuvemshop" in text or "tiendanube" in text

    def _listing_url(self, page: int) -> str:
        path = "/" + self.store.listing_path.strip("/") + "/"
        if page > 1:
            path += f"page/{page}/"
        # ordem por data de criação deixa a paginação estável entre as requisições
        return f"{self.store.url}{path}?results_only=true&sort_by=created-ascending"

    def scan(self) -> ScanResult:
        products: Dict[str, Product] = {}
        complete = True
        sizes: List[int] = []
        for page in range(1, MAX_PAGES + 1):
            try:
                resp = self.fetcher.get(self._listing_url(page))
            except FetchError as e:
                if page == 1:
                    raise
                self.log.warning(f"página {page} da listagem falhou ({e}); varredura incompleta")
                complete = False
                break
            if is_password_page(resp.url):
                return ScanResult(status="password")
            items, errors = parse_listing(resp.text)
            if errors:
                self.log.warning(f"página {page}: {errors} produto(s) com JSON ilegível; varredura incompleta")
                complete = False
            if not items:
                if page == 1:
                    raise AdapterError("nenhum produto encontrado na listagem (loja vazia ou tema não suportado)")
                break
            sizes.append(len(items))
            new_ids = [p.id for p in items if p.id not in products]
            for p in items:
                products[p.id] = p
            if not new_ids:
                self.log.warning(f"página {page} repetiu produtos já vistos; varredura incompleta")
                complete = False
                break
        else:
            self.log.warning(f"parou no limite de {MAX_PAGES} páginas; varredura incompleta")
            complete = False
        if not consistent_page_sizes(sizes):
            # Já vimos o servidor trocar de 50 para 9 itens por página entre respostas
            # (cache). Se isso acontece no meio da varredura, produtos podem ser pulados.
            self.log.warning(f"tamanho das páginas variou ({sizes}); varredura incompleta")
            complete = False
        return ScanResult(status="open", products=products, complete=complete)

    def fetch_variants(self, product_id: str, url: str) -> Optional[Dict[str, Variant]]:
        resp = self.fetcher.get(url)
        return parse_variants(resp.text, product_id)


def consistent_page_sizes(sizes: List[int]) -> bool:
    """Todas as páginas têm o mesmo tamanho, exceto a última (que pode ser menor)."""
    if len(sizes) < 2:
        return True
    first = sizes[0]
    return all(n == first for n in sizes[:-1]) and sizes[-1] <= first


def parse_listing(text: str) -> Tuple[List[Product], int]:
    """Extrai os produtos de uma página de listagem. Retorna (produtos, nº de erros)."""
    stock_attr = dict(STOCK_RE.findall(text))
    positions = [(m.group(1), m.start()) for m in ITEM_RE.finditer(text)]
    products: Dict[str, Product] = {}
    errors = 0
    for i, (pid, start) in enumerate(positions):
        if pid in products:
            continue
        end = positions[i + 1][1] if i + 1 < len(positions) else len(text)
        m = LD_RE.search(text, start, end)
        if not m:
            continue
        try:
            data = json.loads(m.group(1), strict=False)
            products[pid] = _product_from_ld(pid, data, stock_attr.get(pid))
        except (ValueError, KeyError, TypeError):
            errors += 1
    return list(products.values()), errors


def _product_from_ld(pid: str, data: dict, stock_raw: Optional[str]) -> Product:
    offers = data.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    url = offers.get("url") or (data.get("mainEntityOfPage") or {}).get("@id") or data.get("url")
    if not url or not str(url).startswith(("https://", "http://")):
        raise KeyError("url")
    if stock_raw is None:
        stock_raw = (offers.get("inventoryLevel") or {}).get("value")
    stock = parse_stock(stock_raw)
    availability = str(offers.get("availability") or "")
    if availability:
        available = any(marker in availability for marker in AVAILABLE_MARKERS)
    else:
        available = stock is None or stock > 0
    return Product(
        id=pid,
        name=clean_text(html.unescape(str(data.get("name") or ""))),
        url=url,
        image=jpg_image(data.get("image")),
        price=_float(offers.get("price")),
        available=available,
        stock=stock,
    )


def parse_variants(text: str, product_id: str) -> Optional[Dict[str, Variant]]:
    """Lê o data-variants da página do produto (ignora produtos relacionados)."""
    for m in VARIANTS_RE.finditer(text):
        try:
            data = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        if not isinstance(data, list):
            continue
        mine = [v for v in data if str(v.get("product_id")) == str(product_id)]
        if not mine:
            continue
        variants = {}
        for v in mine:
            stock = parse_stock(v.get("stock"))
            name = clean_text(" / ".join(html.unescape(str(v[k])) for k in ("option0", "option1", "option2") if v.get(k)))
            variants[str(v["id"])] = Variant(
                id=str(v["id"]),
                name=name,
                available=bool(v.get("available", stock is None or stock > 0)),
                stock=stock,
            )
        return variants
    return None


def parse_stock(raw) -> Optional[int]:
    if raw is None or raw == "" or str(raw).lower() == "infinite":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def jpg_image(img) -> Optional[str]:
    """O CDN da Nuvemshop entrega .jpg trocando a extensão; o Telegram lida mal com .webp."""
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    if not img:
        return None
    img = str(img)
    if img.startswith("//"):
        img = "https:" + img
    elif img.startswith("http://"):
        img = "https://" + img[len("http://"):]
    if not img.startswith("https://"):
        return None
    return IMG_SIZE_RE.sub("-640-0.jpg", img)


def _float(value) -> Optional[float]:
    try:
        return round(float(value), 2) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
