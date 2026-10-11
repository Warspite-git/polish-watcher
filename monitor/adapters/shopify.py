"""Shopify: catálogo completo via /products.json (250 produtos por página).

O /products.json informa se cada variante está disponível, mas não a quantidade
em estoque. Por isso o alerta de "estoque baixo" não funciona em lojas Shopify.
"""
from __future__ import annotations

from typing import Dict, Optional
from urllib.parse import quote

from ..fetcher import is_password_page
from ..models import Product, ScanResult, Variant, clean_text
from .base import Adapter, AdapterError

PAGE_SIZE = 250
MAX_PAGES = 100


class ShopifyAdapter(Adapter):
    name = "shopify"
    variants_in_listing = True
    has_stock_count = False

    @staticmethod
    def detect(resp) -> bool:
        if any(h.lower().startswith("x-shopify") or h.lower() == "x-shopid" for h in resp.headers):
            return True
        text = resp.text
        return "cdn.shopify.com" in text or "Shopify.shop" in text

    def scan(self) -> ScanResult:
        products: Dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            resp = self.fetcher.get(f"{self.store.url}/products.json?limit={PAGE_SIZE}&page={page}")
            if is_password_page(resp.url):
                return ScanResult(status="password")
            try:
                items = resp.json().get("products", [])
            except ValueError:
                raise AdapterError("/products.json não retornou JSON (loja não é Shopify ou bloqueou o acesso)")
            for raw in items:
                p = parse_product(raw, self.store.url)
                products[p.id] = p
            if len(items) < PAGE_SIZE:
                break
        else:
            self.log.warning(f"parou no limite de {MAX_PAGES} páginas")
            return ScanResult(status="open", products=products, complete=False)
        if not products:
            raise AdapterError("nenhum produto encontrado em /products.json")
        return ScanResult(status="open", products=products, complete=True)


def parse_product(raw: dict, base_url: str) -> Product:
    variants: Dict[str, Variant] = {}
    prices = []
    quantities = []
    for v in raw.get("variants") or []:
        title = clean_text(v.get("title"))
        qty = v.get("inventory_quantity")  # quase nunca é exposto, mas usamos se vier
        variants[str(v["id"])] = Variant(
            id=str(v["id"]),
            name="" if title == "Default Title" else title,
            available=bool(v.get("available")),
            stock=int(qty) if isinstance(qty, (int, float)) else None,
        )
        if v.get("price") not in (None, ""):
            prices.append((bool(v.get("available")), float(v["price"])))
        quantities.append(qty)
    available = any(v.available for v in variants.values())
    # preço: o menor entre as variantes disponíveis (ou entre todas, se esgotado)
    pool = [p for a, p in prices if a] or [p for _, p in prices]
    stock = None
    if quantities and all(isinstance(q, (int, float)) for q in quantities):
        stock = int(sum(max(0, q) for q in quantities))
    images = raw.get("images") or []
    return Product(
        id=str(raw["id"]),
        name=clean_text(raw.get("title")),
        url=f"{base_url}/products/{quote(str(raw.get('handle') or ''), safe='-_.~')}",
        image=_image(images[0].get("src")) if images else None,
        price=round(min(pool), 2) if pool else None,
        available=available,
        stock=stock,
        variants=variants,
    )


def _image(src: Optional[str]) -> Optional[str]:
    if not src:
        return None
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("https://"):
        return None
    return src + ("&" if "?" in src else "?") + "width=800"
