"""The products, in the order a tile needs them: wind first (its building
count decides which tiles get the rest), then the road noise on the same
buildings and roads, the night sky's brightness, heat by morning and by
night and air, then the street air built on air."""

from .heat import Heat
from .wind import Wind
from .air import Air
from .air_street import AirStreet
from .heat_night import HeatNight
from .noise import Noise
from .light import Light

PRODUCTS = {p.name: p for p in (Wind(), Noise(), Light(), Heat(), HeatNight(), Air(), AirStreet())}
ORDER = ["wind", "noise", "light", "heat", "heat_night", "air", "air_street"]
