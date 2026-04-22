"""MT5 adapter boundary for System C bot V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from runtime.config import RuntimeConfig


class Mt5Adapter(Protocol):
    name: str

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def account_info(self) -> dict[str, Any] | None: ...
    def positions_get(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def orders_get(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def history_deals_get(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def symbol_info(self, symbol: str) -> dict[str, Any] | None: ...
    def symbol_info_tick(self, symbol: str) -> dict[str, Any] | None: ...
    def copy_rates_from_pos(self, symbol: str, timeframe: Any, start_pos: int, count: int) -> list[Any]: ...
    def order_send(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def last_error(self) -> Any: ...
    def terminal_info(self) -> dict[str, Any] | None: ...
    def timeframe_value(self, timeframe: str) -> Any: ...


def object_to_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if hasattr(value, "__dict__"):
        return {
            key: attr
            for key in dir(value)
            if not key.startswith("_")
            for attr in [getattr(value, key, None)]
            if not callable(attr)
        }
    return {"value": value}


def sequence_to_dicts(values: Any) -> list[dict[str, Any]]:
    if not values:
        return []
    return [item for item in (object_to_dict(value) for value in values) if item is not None]


@dataclass
class NativeMt5Adapter:
    cfg: RuntimeConfig
    name: str = "native"

    def __post_init__(self) -> None:
        self.mt5 = None

    def connect(self) -> None:
        import MetaTrader5 as mt5

        self.mt5 = mt5
        secrets = self.cfg.secrets
        kwargs = {
            "login": int(secrets["MT5_LOGIN"]),
            "password": secrets["MT5_PASSWORD"],
            "server": secrets["MT5_SERVER"],
        }
        mt5_path = secrets.get("MT5_PATH")
        if mt5_path:
            kwargs["path"] = mt5_path
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    def close(self) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()

    def account_info(self) -> dict[str, Any] | None:
        return object_to_dict(self.mt5.account_info())

    def positions_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return sequence_to_dicts(self.mt5.positions_get(**kwargs) or [])

    def orders_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return sequence_to_dicts(self.mt5.orders_get(**kwargs) or [])

    def history_deals_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return sequence_to_dicts(self.mt5.history_deals_get(**kwargs) or [])

    def symbol_info(self, symbol: str) -> dict[str, Any] | None:
        return object_to_dict(self.mt5.symbol_info(symbol))

    def symbol_info_tick(self, symbol: str) -> dict[str, Any] | None:
        return object_to_dict(self.mt5.symbol_info_tick(symbol))

    def copy_rates_from_pos(self, symbol: str, timeframe: Any, start_pos: int, count: int) -> list[Any]:
        rates = self.mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
        return rates if rates is not None else []

    def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        return object_to_dict(self.mt5.order_send(request)) or {}

    def last_error(self) -> Any:
        return self.mt5.last_error()

    def terminal_info(self) -> dict[str, Any] | None:
        return object_to_dict(self.mt5.terminal_info())

    def timeframe_value(self, timeframe: str) -> Any:
        return getattr(self.mt5, mt5_timeframe_attr(timeframe))


@dataclass
class RpycMt5Adapter:
    cfg: RuntimeConfig
    name: str = "rpyc"

    def __post_init__(self) -> None:
        self.conn = None
        self.mt5 = None
        self.obtain = None

    def _materialize(self, value: Any) -> Any:
        if self.obtain is None:
            return value
        try:
            return self.obtain(value)
        except Exception:
            return value

    def connect(self) -> None:
        import rpyc
        from rpyc.utils.classic import obtain

        self.obtain = obtain
        self.conn = rpyc.classic.connect(
            self.cfg.secrets.get("RPYC_HOST", "localhost"),
            int(self.cfg.secrets.get("RPYC_PORT", 18812)),
        )
        self.mt5 = self.conn.modules.MetaTrader5
        ok = self.mt5.initialize(
            login=int(self.cfg.secrets["MT5_LOGIN"]),
            password=self.cfg.secrets["MT5_PASSWORD"],
            server=self.cfg.secrets["MT5_SERVER"],
        )
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")

    def close(self) -> None:
        if self.mt5 is not None:
            try:
                self.mt5.shutdown()
            except Exception:
                pass
        if self.conn is not None:
            self.conn.close()

    def account_info(self) -> dict[str, Any] | None:
        return object_to_dict(self._materialize(self.mt5.account_info()))

    def positions_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return sequence_to_dicts(self._materialize(self.mt5.positions_get(**kwargs) or []))

    def orders_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return sequence_to_dicts(self._materialize(self.mt5.orders_get(**kwargs) or []))

    def history_deals_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return sequence_to_dicts(self._materialize(self.mt5.history_deals_get(**kwargs) or []))

    def symbol_info(self, symbol: str) -> dict[str, Any] | None:
        return object_to_dict(self._materialize(self.mt5.symbol_info(symbol)))

    def symbol_info_tick(self, symbol: str) -> dict[str, Any] | None:
        return object_to_dict(self._materialize(self.mt5.symbol_info_tick(symbol)))

    def copy_rates_from_pos(self, symbol: str, timeframe: Any, start_pos: int, count: int) -> list[Any]:
        rates = self.mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
        materialized = self._materialize(rates)
        return materialized if materialized is not None else []

    def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        return object_to_dict(self._materialize(self.mt5.order_send(request))) or {}

    def last_error(self) -> Any:
        return self._materialize(self.mt5.last_error())

    def terminal_info(self) -> dict[str, Any] | None:
        return object_to_dict(self._materialize(self.mt5.terminal_info()))

    def timeframe_value(self, timeframe: str) -> Any:
        return getattr(self.mt5, mt5_timeframe_attr(timeframe))


def mt5_timeframe_attr(timeframe: str) -> str:
    normalized = str(timeframe).lower().strip()
    mapping = {
        "1min": "TIMEFRAME_M1",
        "m1": "TIMEFRAME_M1",
        "1m": "TIMEFRAME_M1",
        "5m": "TIMEFRAME_M5",
        "m5": "TIMEFRAME_M5",
        "15m": "TIMEFRAME_M15",
        "m15": "TIMEFRAME_M15",
        "1h": "TIMEFRAME_H1",
        "h1": "TIMEFRAME_H1",
        "4h": "TIMEFRAME_H4",
        "h4": "TIMEFRAME_H4",
    }
    if normalized not in mapping:
        raise ValueError(f"unsupported MT5 timeframe: {timeframe}")
    return mapping[normalized]
