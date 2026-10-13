from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


def clean_text(value) -> str:
    """Normaliza textos vindos das lojas: uma linha só, sem caracteres de controle.

    Evita que um nome de produto com quebra de linha injete comandos de workflow
    ("::comando::") nos logs públicos do GitHub Actions ou quebre a formatação.
    """
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch.isprintable())


@dataclass
class Variant:
    id: str
    name: str = ""
    available: bool = True
    stock: Optional[int] = None  # None = sem controle de estoque / desconhecido


@dataclass
class Product:
    id: str
    name: str
    url: str
    image: Optional[str] = None
    price: Optional[float] = None
    available: bool = True
    stock: Optional[int] = None  # None = sem controle de estoque / desconhecido
    # None = a listagem não informa variantes (precisa abrir a página do produto)
    variants: Optional[Dict[str, Variant]] = None
    added_at: Optional[str] = None  # quando o produto entrou na loja (ISO, UTC), se der para saber
    added_at_exact: bool = False    # False = estimativa (ex.: data de upload da foto)


@dataclass
class ScanResult:
    status: str  # "open" | "password"
    products: Dict[str, Product] = field(default_factory=dict)
    complete: bool = True  # True se todas as páginas foram lidas sem erro


# Tipos de alerta
NEW = "new"
RESTOCK = "restock"
VARIANT_RESTOCK = "variant_restock"
REPLENISH = "replenish"  # reposição: mais unidades num produto que não chegou a esgotar
LOW_STOCK = "low_stock"
PRICE = "price"
RETRACT = "retract"    # apagar do grupo o alerta de um produto que a loja retirou do ar
SOLD_OUT = "sold_out"  # resposta "⚡ esgotou" ao alerta que anunciou o produto
STORE_OPEN = "store_open"
STORE_CLOSED = "store_closed"
STORE_ERROR = "store_error"
STORE_RECOVERED = "store_recovered"

STORE_KINDS = {STORE_OPEN, STORE_CLOSED, STORE_ERROR, STORE_RECOVERED}

# Rótulos usados nos logs e no resumo do Actions
LABELS = {
    NEW: "novo(s)",
    RESTOCK: "restock(s)",
    VARIANT_RESTOCK: "restock(s) de variante",
    REPLENISH: "reposição(ões)",
    LOW_STOCK: "estoque baixo",
    PRICE: "mudança(s) de preço",
    SOLD_OUT: "esgotado(s)",
    RETRACT: "alerta(s) apagado(s) (produto retirado)",
    STORE_OPEN: "loja abriu",
    STORE_CLOSED: "loja fechou",
    STORE_ERROR: "aviso de erro",
    STORE_RECOVERED: "loja recuperada",
}


def describe_counts(kinds) -> str:
    counts = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{n} {LABELS.get(k, k)}" for k, n in counts.items())


@dataclass
class Alert:
    kind: str
    store: str
    store_url: str
    name: str = ""
    url: str = ""
    image: Optional[str] = None
    price: Optional[float] = None
    old_price: Optional[float] = None
    old_stock: Optional[int] = None
    stock: Optional[int] = None
    available: bool = True
    variants: List[str] = field(default_factory=list)
    note: str = ""
    created: str = ""
    store_key: str = ""            # domínio da loja (arquivo de estado)
    store_tag: str = ""            # hashtag da loja; vazio = gerada a partir do nome
    product_id: str = ""
    reply_to: Optional[int] = None  # responder a esta mensagem do Telegram
    message_id: Optional[int] = None  # preenchido depois do envio

    @property
    def is_store_alert(self) -> bool:
        return self.kind in STORE_KINDS

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Alert":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)
