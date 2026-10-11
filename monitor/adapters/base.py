from __future__ import annotations

import logging
from typing import Dict, Optional

import requests

from ..config import StoreConfig
from ..fetcher import Fetcher
from ..models import ScanResult, Variant


class AdapterError(Exception):
    pass


class Adapter:
    """Base para os adaptadores de plataforma.

    Para criar uma nova plataforma: subclasse, defina `name`, `detect` e `scan`
    e registre a classe em adapters/__init__.py.
    """

    name = "base"
    # True se a listagem já traz as variantes (Shopify). Se False, o monitor abre a
    # página do produto quando precisa de detalhes por variante (Nuvemshop).
    variants_in_listing = True
    # True se a plataforma informa a quantidade em estoque (para "estoque baixo").
    has_stock_count = True

    def __init__(self, store: StoreConfig, fetcher: Fetcher, log: Optional[logging.LoggerAdapter] = None):
        self.store = store
        self.fetcher = fetcher
        self.log = log or logging.getLogger(__name__)

    @staticmethod
    def detect(resp: requests.Response) -> bool:
        raise NotImplementedError

    def scan(self) -> ScanResult:
        raise NotImplementedError

    def fetch_variants(self, product_id: str, url: str) -> Optional[Dict[str, Variant]]:
        return None
