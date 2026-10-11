from __future__ import annotations

from typing import Dict, Type

from ..fetcher import Fetcher
from .base import Adapter, AdapterError
from .nuvemshop import NuvemshopAdapter
from .shopify import ShopifyAdapter

# Para adicionar uma plataforma: crie o adaptador e registre aqui.
# A ordem importa na detecção automática (assinaturas mais específicas primeiro).
ADAPTERS: Dict[str, Type[Adapter]] = {
    ShopifyAdapter.name: ShopifyAdapter,
    NuvemshopAdapter.name: NuvemshopAdapter,
}


def detect_platform(fetcher: Fetcher, url: str) -> str:
    """Abre a página inicial e procura assinaturas de cada plataforma."""
    resp = fetcher.get(url + "/")
    for name, cls in ADAPTERS.items():
        if cls.detect(resp):
            return name
    raise AdapterError(
        "plataforma não reconhecida; defina 'plataforma' no sites.yaml "
        f"({', '.join(ADAPTERS)}) ou crie um adaptador novo"
    )


__all__ = ["ADAPTERS", "Adapter", "AdapterError", "detect_platform"]
