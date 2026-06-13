from datetime import datetime
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

class Tristate(str, Enum):
    YES = "Да"
    NO = "Нет"
    UNKNOWN = "Нет данных"

class ParkingRecord(BaseModel):
    # --- Базовые поля ---
    id_2gis: str
    name: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    url: str
    
    # --- Поля условий и цен ---
    is_paid: Optional[bool] = None
    price_per_hour: Optional[float] = None
    raw_tariff: Optional[str] = None
    capacity: Optional[int] = None
    
    # --- НОВЫЕ ПОЛЯ (Гео-аналитика и фичи) ---
    district: Optional[str] = "Нет данных"          # В каком районе Алматы
    nearby_stations: Optional[str] = "Нет данных"   # Станция метро или ключевые точки рядом
    for_trucks: Tristate = Tristate.UNKNOWN        # Подходит ли для грузовиков
    paving_type: Optional[str] = "Нет данных"      # Тип покрытия (асфальт, грунт и т.д., если прилетит)
    
    # --- Социальные фичи  ---
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    has_disabled_parking: Tristate = Tristate.UNKNOWN
    has_ev_charging: Tristate = Tristate.UNKNOWN
    
    parsed_at: datetime = Field(default_factory=datetime.utcnow)