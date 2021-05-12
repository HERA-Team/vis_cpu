# -*- coding: utf-8 -*-
"""A fast visibility simulator based on per-antenna calculations."""
from pkg_resources import DistributionNotFound, get_distribution

try:
    # Change here if project is renamed and does not equal the package name
    dist_name = __name__
    __version__ = get_distribution(dist_name).version
except DistributionNotFound:
    __version__ = "unknown"
finally:
    del get_distribution, DistributionNotFound

from .vis_cpu import vis_cpu
from .vis_gpu import vis_gpu
