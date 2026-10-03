"""The products, in the order a tile needs them: wind first (its building
count decides which tiles get the rest), then heat and air, then the street
air built on air."""

from .heat import Heat
from .wind import Wind
from .air import Air
from .air_street import AirStreet

PRODUCTS = {p.name: p for p in (Wind(), Heat(), Air(), AirStreet())}
ORDER = ["wind", "heat", "air", "air_street"]
