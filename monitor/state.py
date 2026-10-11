"""Estado salvo em state/<loja>.json, commitado de volta pelo workflow.

Um produto por linha, para os diffs dos commits ficarem pequenos e legíveis.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .models import Alert

STATE_DIR = Path("state")
OUTBOX = "_outbox.json"  # alertas que falharam no envio e serão reenviados


def new_state() -> dict:
    return {"platform": None, "baseline": None, "status": None, "errors": 0, "products": {}}


def load(key: str, state_dir: Path = STATE_DIR) -> dict:
    path = state_dir / f"{key}.json"
    if not path.exists():
        return new_state()
    state = new_state()
    state.update(json.loads(path.read_text(encoding="utf-8")))
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


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save(key: str, state: dict, state_dir: Path = STATE_DIR):
    _write(state_dir / f"{key}.json", dumps(state))


def load_outbox(state_dir: Path = STATE_DIR) -> List[Alert]:
    path = state_dir / OUTBOX
    if not path.exists():
        return []
    return [Alert.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def save_outbox(alerts: List[Alert], state_dir: Path = STATE_DIR):
    path = state_dir / OUTBOX
    if not alerts:
        if path.exists():
            path.unlink()
        return
    _write(path, json.dumps([a.to_dict() for a in alerts], ensure_ascii=False, indent=1) + "\n")
