"""The products, in the order a tile needs them: wind first (its building
count decides which tiles get the rest), then the road noise on the same
buildings and roads, the night sky's brightness, where and when the ground
was built, heat by morning and by night and air, then the street air built
on air; last, the mornings of earlier decades, with what time is left."""

from .heat import Heat
from .wind import Wind
from .air import Air
from .air_street import AirStreet
from .heat_night import HeatNight
from .noise import Noise
from .heat_past import PAST
from .air_past import AirPast
from .light import Light
from .built import Built

PRODUCTS = {p.name: p for p in (Wind(), Noise(), Light(), Built(), Heat(), HeatNight(), Air(), AirStreet(), *PAST, AirPast())}
ORDER = ["wind", "noise", "light", "built", "heat", "heat_night", "air", "air_street", *(p.name for p in PAST), "air_past"]
