"""ローカル実行用の状態保存。同時起動やAzure上の永続化には使わない。"""

import json
import os
import re
from pathlib import Path

from .models import DeliveryState


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> DeliveryState:
        if not self._path.exists():
            return DeliveryState()
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid state file")

        # 状態破損を新規状態扱いすると予算・既読が失われるため、読み込みを止めます。
        month = data.get("month")
        attempts = data.get("translation_attempts")
        urls = data.get("delivered_urls")
        if (
            not isinstance(month, str)
            or (month != "" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month))
            or type(attempts) is not int
            or not 0 <= attempts <= 30
            or not isinstance(urls, list)
            or not all(isinstance(url, str) for url in urls)
        ):
            raise ValueError("Invalid state; restore a valid backup before retrying")
        return DeliveryState(month, attempts, set(urls))

    def save(self, state: DeliveryState) -> None:
        data = {
            "month": state.month,
            "translation_attempts": state.translation_attempts,
            "delivered_urls": sorted(state.delivered_urls),
        }
        temporary_path = self._path.with_name(self._path.name + ".tmp")
        temporary_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary_path, self._path)
