"""Leitura e validação do sites.yaml."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import yaml

PLATFORMS = ("auto", "nuvemshop", "shopify")


class ConfigError(Exception):
    pass


@dataclass
class AlertConfig:
    new: bool = True
    restock: bool = True
    low_stock: Optional[int] = None  # limite; None = desligado
    replenish: Optional[int] = None  # mínimo de unidades novas para avisar reposição; None = desligado
    price_change: bool = False
    store_status: bool = True
    sold_out: bool = True  # responde ao alerta quando o produto esgota


@dataclass
class StoreConfig:
    name: str
    url: str  # base normalizada: https://host
    key: str  # host sem "www." — também é o nome do arquivo de estado
    platform: str = "auto"
    delay: float = 1.5
    alerts: AlertConfig = field(default_factory=AlertConfig)
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    enabled: bool = True
    listing_path: str = "/produtos/"
    logo: str = ""  # imagem usada quando o produto não tem foto (vazio = tenta achar no site)
    hashtag: str = ""  # vazio = gerada do nome (ex.: "By Dany Vianna" → #ByDanyVianna); várias separadas por espaço

    def wants(self, product_name: str) -> bool:
        """Aplica os filtros de palavra-chave (sem diferenciar maiúsculas/acentos)."""
        name = normalize(product_name)
        if self.include and not any(normalize(k) in name for k in self.include):
            return False
        if any(normalize(k) in name for k in self.exclude):
            return False
        return True


@dataclass
class Settings:
    stores: List[StoreConfig]
    max_individual_alerts: int = 10
    error_alert_after: int = 8
    max_product_pages: int = 15
    urgent_stock: int = 5  # selo "🔥 Só N unidades" quando o estoque está no limite ou abaixo
    show_stock: bool = True  # False = alertas não mostram a quantidade em estoque


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def normalize_url(raw: str):
    raw = str(raw).strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise ConfigError(f"URL inválida: {raw!r}")
    host = parsed.netloc.lower()
    base = f"{parsed.scheme or 'https'}://{host}"
    key = host[4:] if host.startswith("www.") else host
    return base, key


def _parse_threshold(value, key: str, default_if_true: int) -> Optional[int]:
    """false/None → desligado; true → valor padrão; número → o próprio número."""
    if value is None or value is False:
        return None
    if value is True:
        return default_if_true
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} deve ser false ou um número, recebi {value!r}")
    return n if n > 0 else None


def _alerts(raw: dict, base: AlertConfig) -> AlertConfig:
    raw = raw or {}
    return AlertConfig(
        new=bool(raw.get("novos", base.new)),
        restock=bool(raw.get("restock", base.restock)),
        low_stock=_parse_threshold(raw["estoque_baixo"], "estoque_baixo", 3) if "estoque_baixo" in raw else base.low_stock,
        replenish=_parse_threshold(raw["reposicao"], "reposicao", 5) if "reposicao" in raw else base.replenish,
        price_change=bool(raw.get("preco", base.price_change)),
        store_status=bool(raw.get("loja_fechada", base.store_status)),
        sold_out=bool(raw.get("esgotou", base.sold_out)),
    )


def _words(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value if str(v).strip()]


def load_settings(path: str = "sites.yaml") -> Settings:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Arquivo de configuração não encontrado: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    defaults = data.get("padrao") or {}
    default_alerts = _alerts(defaults.get("alertas"), AlertConfig())
    default_filters = defaults.get("filtros") or {}
    default_platform = str(defaults.get("plataforma", "auto")).lower()
    default_delay = float(defaults.get("pausa_segundos", 1.5))

    stores: List[StoreConfig] = []
    seen_keys = set()
    for i, raw in enumerate(data.get("lojas") or [], start=1):
        if not isinstance(raw, dict) or not raw.get("url"):
            raise ConfigError(f"Loja #{i} sem 'url' no sites.yaml")
        base, key = normalize_url(raw["url"])
        if key in seen_keys:
            raise ConfigError(f"Loja repetida no sites.yaml: {key}")
        seen_keys.add(key)
        platform = str(raw.get("plataforma", default_platform)).lower()
        if platform not in PLATFORMS:
            raise ConfigError(f"Plataforma desconhecida '{platform}' em {key}. Use: {', '.join(PLATFORMS)}")
        filters = raw.get("filtros") or {}
        stores.append(StoreConfig(
            name=str(raw.get("nome") or key),
            url=base,
            key=key,
            platform=platform,
            delay=float(raw.get("pausa_segundos", default_delay)),
            alerts=_alerts(raw.get("alertas"), default_alerts),
            include=_words(filters.get("incluir", default_filters.get("incluir"))),
            exclude=_words(filters.get("excluir", default_filters.get("excluir"))),
            enabled=bool(raw.get("ativo", True)),
            listing_path=str(raw.get("caminho_listagem", "/produtos/")),
            logo=str(raw.get("logo") or ""),
            hashtag=" ".join(str(t).strip().lstrip("#") for t in _words(raw.get("hashtag") or raw.get("hashtags"))),
        ))

    tg = data.get("telegram") or {}
    adv = data.get("avancado") or {}
    return Settings(
        stores=stores,
        max_individual_alerts=int(tg.get("max_alertas_individuais", 10)),
        urgent_stock=int(tg.get("urgencia_estoque", 5)),
        show_stock=bool(tg.get("mostrar_estoque", True)),
        error_alert_after=int(adv.get("falhas_para_avisar", 8)),
        max_product_pages=int(adv.get("max_paginas_produto", 15)),
    )
