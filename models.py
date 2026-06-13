from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Tristate(str, Enum):
    YES = "Да"
    NO = "Нет"
    UNKNOWN = "Нет данных"


class ParkingRecord(BaseModel):
    # --- Идентификация ---
    id_2gis: str
    name: str
    url: str

    # --- Местоположение ---
    address: Optional[str] = None
    latitude: float
    longitude: float
    district: Optional[str] = None       # район Алматы из adm_div

    # --- Классификация ---
    # городская / БЦ / ТЦ / ЖК / частная / неизвестно
    type: Optional[str] = None
    target_object: Optional[str] = None  # к какому объекту относится (ТРК АДК, БЦ Нурлы Тау…)

    # --- Условия ---
    is_paid: Optional[bool] = None
    price_per_hour: Optional[float] = None   # числовое поле для фильтрации
    raw_tariff: Optional[str] = None         # текст как есть в 2ГИС
    capacity: Optional[int] = None

    # --- Режим работы ---
    working_hours: Optional[str] = None

    # --- Особенности ---
    for_trucks: Tristate = Tristate.UNKNOWN
    paving_type: Optional[str] = None
    has_disabled_parking: Tristate = Tristate.UNKNOWN
    has_ev_charging: Tristate = Tristate.UNKNOWN

    # --- Социальные метрики ---
    rating: Optional[float] = None
    reviews_count: Optional[int] = None

    # --- Служебное ---
    parsed_at: datetime = Field(default_factory=datetime.utcnow)
