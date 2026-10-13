from monitor.adapters.nuvemshop import jpg_image, parse_listing, parse_stock, parse_variants


def item(pid, stock_attr, ld):
    return f"""
<div class="js-item-product item" data-product-id="{pid}" data-component="product-list-item">
  <a href="#" class="js-item-name">x</a>
  <span class="hidden" data-store="stock-product-{pid}-{stock_attr}"></span>
  <script type="application/ld+json" data-component='structured-data.item'>
  {ld}
  </script>
</div>"""


LISTING = "<div class='grid-row'>" + "".join([
    item("111", "30", """{
        "@context": "https://schema.org/", "@type": "Product",
        "mainEntityOfPage": {"@type": "WebPage", "@id": "https://loja.test/produtos/poseidon/"},
        "name": "DVW - Poseidon&#039;s Claim",
        "image": "https://acdn-us.mitiendanube.com/stores/1/products/a-480-0.webp",
        "description": "Linha 1
linha 2",
        "offers": {"@type": "Offer", "url": "https://loja.test/produtos/poseidon/", "priceCurrency": "BRL",
                   "price": "65", "availability": "https://schema.org/InStock",
                   "inventoryLevel": {"@type": "QuantitativeValue", "value": "30"}}}"""),
    item("222", "0", """{
        "@type": "Product", "name": "Salt &amp; Stars",
        "image": "https://acdn-us.mitiendanube.com/stores/1/products/b-480-0.jpeg",
        "offers": {"@type": "Offer", "url": "https://loja.test/produtos/salt/", "price": "59.9",
                   "availability": "https://schema.org/OutOfStock",
                   "inventoryLevel": {"@type": "QuantitativeValue", "value": "0"}}}"""),
    item("333", "infinite", """{
        "@type": "Product", "name": "Diamond Flash",
        "image": "//acdn-us.mitiendanube.com/stores/1/products/c-480-0.png",
        "offers": {"@type": "Offer", "url": "https://loja.test/produtos/diamond/", "price": "25",
                   "availability": "https://schema.org/InStock"}}"""),
]) + "</div>"


def test_parse_listing():
    products, errors = parse_listing(LISTING)
    assert errors == 0
    by_id = {p.id: p for p in products}
    assert set(by_id) == {"111", "222", "333"}

    a = by_id["111"]
    assert a.name == "DVW - Poseidon's Claim"
    assert a.url == "https://loja.test/produtos/poseidon/"
    assert a.price == 65.0 and a.stock == 30 and a.available
    assert a.image == "https://acdn-us.mitiendanube.com/stores/1/products/a-640-0.jpg"

    b = by_id["222"]
    assert b.name == "Salt & Stars" and b.stock == 0 and not b.available and b.price == 59.9

    c = by_id["333"]
    assert c.stock is None and c.available  # estoque infinito
    assert c.image == "https://acdn-us.mitiendanube.com/stores/1/products/c-640-0.jpg"
    assert c.variants is None  # listagem não traz variantes


def test_parse_listing_counts_broken_json():
    html = item("1", "3", "{ isto não é json }") + item("2", "3", """{"name": "Ok",
        "offers": {"url": "https://loja.test/produtos/ok/", "price": "1", "availability": "InStock"}}""")
    products, errors = parse_listing(html)
    assert errors == 1
    assert [p.id for p in products] == ["2"]


def test_parse_listing_empty():
    assert parse_listing("<html>nada</html>") == ([], 0)


VARIANTS_RELATED = ('[{&quot;product_id&quot;:999,&quot;id&quot;:1,&quot;stock&quot;:5,&quot;available&quot;:true,'
                    '&quot;option0&quot;:null}]')
VARIANTS_MAIN = ('[{&quot;product_id&quot;:111,&quot;id&quot;:10,&quot;stock&quot;:0,&quot;available&quot;:false,'
                 '&quot;option0&quot;:&quot;Mini&quot;,&quot;option1&quot;:null},'
                 '{&quot;product_id&quot;:111,&quot;id&quot;:11,&quot;stock&quot;:null,&quot;available&quot;:true,'
                 '&quot;option0&quot;:&quot;Full&quot;,&quot;option1&quot;:&quot;Holo&quot;}]')


def test_parse_variants_ignores_related_products():
    page = f'<div data-variants="{VARIANTS_RELATED}"></div><form data-variants="{VARIANTS_MAIN}"></form>'
    variants = parse_variants(page, "111")
    assert set(variants) == {"10", "11"}
    assert variants["10"].name == "Mini" and variants["10"].stock == 0 and not variants["10"].available
    assert variants["11"].name == "Full / Holo" and variants["11"].stock is None and variants["11"].available
    assert parse_variants(page, "555") is None


def test_helpers():
    assert parse_stock("infinite") is None
    assert parse_stock("12") == 12
    assert parse_stock(None) is None
    assert jpg_image(None) is None
    assert jpg_image("http://x.test/p/a-1024-1024.webp") == "https://x.test/p/a-640-0.jpg"
    # URL sem sufixo de tamanho fica como está
    assert jpg_image("https://x.test/tmp/foto.jpg") == "https://x.test/tmp/foto.jpg"


# ------------------------------------------------------------ varredura com servidor falso

from monitor.adapters.nuvemshop import NuvemshopAdapter, consistent_page_sizes
from monitor.config import StoreConfig


class FakeResp:
    def __init__(self, url, text):
        self.url, self.text = url, text


class FakeFetcher:
    """pages: lista com o nº de itens de cada página (ids sequenciais)."""
    def __init__(self, pages, password=False):
        self.pages, self.password, self.requests = pages, password, 0

    def get(self, url):
        self.requests += 1
        if self.password:
            return FakeResp("https://loja.test/password/", "senha")
        n = int(url.split("/page/")[1].split("/")[0]) if "/page/" in url else 1
        count = self.pages[n - 1] if n <= len(self.pages) else 0
        start = sum(self.pages[: n - 1])
        html = "".join(item(str(start + i), "5", '{"name": "P%d", "offers": {"url": "https://loja.test/p/%d", '
                            '"price": "10", "availability": "InStock"}}' % (start + i, start + i))
                       for i in range(count))
        return FakeResp(url, html)


def adapter(fetcher):
    return NuvemshopAdapter(StoreConfig(name="L", url="https://loja.test", key="loja.test"), fetcher)


def test_scan_paginates_until_empty_page():
    f = FakeFetcher([20, 20, 7])
    res = adapter(f).scan()
    assert res.status == "open" and res.complete and len(res.products) == 47 and f.requests == 4


def test_scan_detects_password():
    assert adapter(FakeFetcher([20], password=True)).scan().status == "password"


def test_scan_inconsistent_page_size_is_incomplete():
    res = adapter(FakeFetcher([9, 50, 50])).scan()
    assert not res.complete


def test_consistent_page_sizes():
    assert consistent_page_sizes([50, 50, 29])
    assert consistent_page_sizes([12])
    assert not consistent_page_sizes([9, 50, 50])
    assert not consistent_page_sizes([20, 20, 30])


def test_image_upload_date():
    from monitor.adapters.nuvemshop import image_upload_date
    url = ("https://acdn-us.mitiendanube.com/stores/294/962/products/"
           "b1461801-24e2-4a02-93ea-e7a20f1ab827-4c4a0541911674e1c717916540182072-640-0.jpg")
    assert image_upload_date(url) == "2026-10-10T17:40:18+00:00"
    assert image_upload_date("https://x.test/tmp/foto.jpg") is None
    assert image_upload_date(None) is None


class FlakyFetcher(FakeFetcher):
    """Cada leitura completa (a partir da página 1) usa a próxima configuração de páginas."""
    def __init__(self, attempts):
        super().__init__(attempts[0])
        self.attempts, self.n = attempts, -1

    def get(self, url):
        if "/page/" not in url:
            self.n += 1
            self.pages = self.attempts[min(self.n, len(self.attempts) - 1)]
        return super().get(url)


def test_scan_rereads_when_page_size_changes_mid_scan():
    # 1ª leitura: o servidor muda de 9 para 12 itens por página (produtos escorregam);
    # 2ª leitura: estável → completa
    f = FlakyFetcher([[9, 9, 12, 12, 3], [12, 12, 12, 9]])
    res = adapter(f).scan()
    assert res.complete and f.n == 1
    assert {str(i) for i in range(45)} <= set(res.products)


def test_placeholder_image_means_no_photo():
    assert jpg_image("https://acdn-us.mitiendanube.com/assets/stores/img/no-photo-480-0.webp") is None
    assert jpg_image("//acdn-us.mitiendanube.com/assets/themes/x/static/images/empty-placeholder.png") is None
