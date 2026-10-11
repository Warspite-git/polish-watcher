from monitor.adapters.shopify import parse_product

RAW = {
    "id": 7179451531453,
    "title": "Suspension Base",
    "handle": "suspension-base",
    "images": [{"src": "https://cdn.shopify.com/s/files/1/base.jpg?v=1"}],
    "variants": [
        {"id": 1, "title": "100 ml", "available": False, "price": "89.90"},
        {"id": 2, "title": "15 ml", "available": True, "price": "39.90"},
    ],
}


def test_parse_product():
    p = parse_product(RAW, "https://www.drknails.com")
    assert p.id == "7179451531453"
    assert p.url == "https://www.drknails.com/products/suspension-base"
    assert p.image == "https://cdn.shopify.com/s/files/1/base.jpg?v=1&width=800"
    assert p.available
    assert p.price == 39.9  # menor preço entre as variantes disponíveis
    assert p.stock is None  # Shopify não expõe quantidade
    assert {v.name: v.available for v in p.variants.values()} == {"100 ml": False, "15 ml": True}


def test_single_variant_and_no_image():
    raw = {"id": 5, "title": "Top Coat", "handle": "top", "images": [],
           "variants": [{"id": 9, "title": "Default Title", "available": False, "price": "50.00"}]}
    p = parse_product(raw, "https://x.test")
    assert not p.available and p.price == 50.0 and p.image is None
    assert p.variants["9"].name == ""
