import pytest


@pytest.fixture(autouse=True)
def no_real_image_downloads(monkeypatch):
    """Os testes nunca baixam imagens de verdade (cada teste simula o que precisa)."""
    import monitor.telegram as tgmod
    monkeypatch.setattr(tgmod, "_download", lambda url, limit=0: None)
