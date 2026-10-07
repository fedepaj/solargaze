"""The products, in the order a tile needs them: wind first (its building
count decides which tiles get the rest), then heat by morning and by night
and air, then the street air built on air."""

from .heat import Heat
from .wind import Wind
from .air import Air
from .air_street import AirStreet
from .heat_night import HeatNight

PRODUCTS = {p.name: p for p in (Wind(), Heat(), HeatNight(), Air(), AirStreet())}
ORDER = ["wind", "heat", "heat_night", "air", "air_street"]
