"""Recognise flight approach/departure points in mixed daily GPS records."""
from datetime import datetime
import numpy as np


def flight_point_mask(points):
    # Repeated fast/high fixes establish airborne context. Keep nearby slow
    # approach/departure fixes out of rail density too; an entire day's mixed
    # train/flight record must never be labelled as one transport mode.
    # A speed spike only counts as an airborne seed where flight is possible:
    # below ~300 m civil aircraft are limited to 250 kt, so a low-altitude
    # >450 km/h fix is a recording artefact and must not condemn the genuine
    # ground segment around it. Missing altitude keeps the previous behaviour.
    times=np.array([datetime.fromisoformat(p['timestamp'].replace('Z','+00:00')).timestamp()
        if p.get('timestamp') else np.nan for p in points])
    def airborne_seed(p):
        if p.get('_accuracy_m',0)>90:return False
        if (p.get('altitude_m') or 0)>6000:return True
        if (p.get('speed_kmh') or 0)<=450:return False
        alt=p.get('altitude_m')
        return alt is None or alt>300
    seeds=np.sort([times[i] for i,p in enumerate(points)
        if np.isfinite(times[i]) and airborne_seed(p)])
    if not len(seeds):return np.zeros(len(points),dtype=bool)
    counts=np.searchsorted(seeds,times+300,side='right')-np.searchsorted(seeds,times-300)
    return np.isfinite(times)&(counts>=3)
