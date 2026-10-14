"""Estado salvo em state/<loja>.json, commitado de volta pelo workflow.

Um produto por linha, para os diffs dos commits ficarem pequenos e legíveis.

Lojas privadas (segredo SITES_PRIVADOS) têm o estado criptografado com a chave
STATE_KEY (Fernet) e salvo com um nome derivado da chave, para que nem o nome do
arquivo revele qual é a loja no repositório público.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import List

from .models import Alert

STATE_DIR = Path("state")
OUTBOX = "_outbox.json"            # alertas que falharam no envio e serão reenviados
PRIVATE_OUTBOX = "_outbox-p.bin"   # idem, de lojas privadas (criptografado)


class StateKeyMissing(Exception):
    pass


def new_state() -> dict:
    return {"platform": None, "baseline": None, "status": None, "errors": 0, "products": {}}


# ---------------------------------------------------------------- criptografia

def has_key() -> bool:
    return bool(os.environ.get("STATE_KEY"))


def _fernet():
    key = os.environ.get("STATE_KEY")
    if not key:
        raise StateKeyMissing("STATE_KEY não configurada")
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _private_path(key: str, state_dir: Path) -> Path:
    secret = os.environ.get("STATE_KEY", "")
    name = hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()[:16]
    return state_dir / f"p-{name}.bin"


def _read_private(path: Path) -> str:
    return _fernet().decrypt(path.read_bytes()).decode("utf-8")


def _write_private(path: Path, text: str):
    if path.exists():
        try:
            if _read_private(path) == text:
                return  # nada mudou: não regrava (o texto cifrado muda a cada gravação e geraria commit)
        except Exception:
            pass
    _write(path, _fernet().encrypt(text.encode("utf-8")))


# ---------------------------------------------------------------- estado

def load(key: str, state_dir: Path = STATE_DIR, private: bool = False) -> dict:
    path = _private_path(key, state_dir) if private else state_dir / f"{key}.json"
    if not path.exists():
        return new_state()
    text = _read_private(path) if private else path.read_text(encoding="utf-8")
    state = new_state()
    state.update(json.loads(text))
    return state


def dumps(state: dict) -> str:
    lines = ["{"]
    for k, v in state.items():
        if k != "products":
            lines.append(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},")
    products = state.get("products") or {}
    lines.append('  "products": {')
    items = sorted(products.items(), key=lambda kv: (len(kv[0]), kv[0]))  # ids numéricos em ordem
    for i, (pid, rec) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"    {json.dumps(pid)}: {json.dumps(rec, ensure_ascii=False)}{comma}")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def save(key: str, state: dict, state_dir: Path = STATE_DIR, private: bool = False):
    if private:
        _write_private(_private_path(key, state_dir), dumps(state))
    else:
        _write(state_dir / f"{key}.json", dumps(state))


# ---------------------------------------------------------------- alertas pendentes

def load_outbox(state_dir: Path = STATE_DIR) -> List[Alert]:
    alerts: List[Alert] = []
    path = state_dir / OUTBOX
    if path.exists():
        alerts += [Alert.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]
    private = state_dir / PRIVATE_OUTBOX
    if private.exists() and has_key():
        try:
            alerts += [Alert.from_dict(d) for d in json.loads(_read_private(private))]
        except Exception:
            pass
    return alerts


def save_outbox(alerts: List[Alert], state_dir: Path = STATE_DIR):
    """Alertas de lojas privadas vão para um arquivo criptografado (o repositório é público)."""
    public = [a for a in alerts if not a.private]
    private = [a for a in alerts if a.private]
    for name, items in ((OUTBOX, public), (PRIVATE_OUTBOX, private)):
        path = state_dir / name
        if not items:
            if path.exists():
                path.unlink()
            continue
        text = json.dumps([a.to_dict() for a in items], ensure_ascii=False, indent=1) + "\n"
        if name == PRIVATE_OUTBOX:
            if has_key():
                _write_private(path, text)
        else:
            _write(path, text)
