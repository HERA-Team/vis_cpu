"""CPU-based implementation of the visibility simulator."""
from __future__ import annotations

import logging
import numpy as np
from astropy.constants import c
from pyuvdata import UVBeam
from re import I
from scipy.interpolate import RectBivariateSpline
from typing import Callable, Optional, Sequence

from . import conversions

# This enables us to put in profile decorators that will be no-ops if no profiling
# library is being used.
try:
    profile
except NameError:

    def profile(fnc):
        """No-op profiling decorator."""
        return fnc


logger = logging.getLogger(__name__)


def _wrangle_beams(beam_idx, beam_list, polarized, nant, freq, interp=True):
    """Perform all the operations and checks on the beams."""
    # Get the number of unique beams
    nbeam = len(beam_list)

    # Check the beam indices
    if beam_idx is None:
        if nbeam == 1:
            beam_idx = np.zeros(nant, dtype=int)
        elif nbeam == nant:
            beam_idx = np.arange(nant, dtype=int)
        else:
            raise ValueError(
                "If number of beams provided is not 1 or nant, beam_idx must be provided."
            )
    else:
        assert beam_idx.shape == (nant,), "beam_idx must be length nant"
        assert all(
            0 <= i < nbeam for i in beam_idx
        ), "beam_idx contains indices greater than the number of beams"

    if interp:
        # make sure we interpolate to the right frequency first.
        beam_list = [
            bm.interp(freq_array=np.array([freq]), new_object=True, run_check=False)
            if isinstance(bm, UVBeam)
            else bm
            for bm in beam_list
        ]

    if polarized and any(b.beam_type != "efield" for b in beam_list):
        raise ValueError("beam type must be efield if using polarized=True")
    elif not polarized and any(
        (
            b.beam_type != "power"
            or getattr(b, "Npols", 1) > 1
            or b.polarization_array[0] not in [-5, -6]
        )
        for b in beam_list
    ):
        raise ValueError(
            "beam type must be power and have only one pol (either xx or yy) if polarized=False"
        )

    return beam_list, nbeam, beam_idx


def _evaluate_beam_cpu(
    beam_list,
    tx,
    ty,
    polarized,
    nbeam,
    nax,
    nfeed,
    freq,
    nsrcs_up,
    complex_dtype,
    spline_opts=None,
):
    A_s = np.zeros((nax, nfeed, nbeam, nsrcs_up), dtype=complex_dtype)

    # Primary beam pattern using direct interpolation of UVBeam object
    az, za = conversions.enu_to_az_za(enu_e=tx, enu_n=ty, orientation="uvbeam")
    for i, bm in enumerate(beam_list):
        kw = (
            {
                "reuse_spline": True,
                "check_azza_domain": False,
                "spline_opts": spline_opts,
            }
            if isinstance(bm, UVBeam)
            else {}
        )

        interp_beam = bm.interp(
            az_array=az,
            za_array=za,
            freq_array=np.atleast_1d(freq),
            **kw,
        )[0]

        if polarized:
            interp_beam = interp_beam[:, 0, :, 0, :]
        else:
            # Here we have already asserted that the beam is a power beam and
            # has only one polarization, so we just evaluate that one.
            interp_beam = np.sqrt(interp_beam[0, 0, 0, 0, :])

        A_s[:, :, i] = interp_beam

        # Check for invalid beam values
        if np.any(np.isinf(A_s)) or np.any(np.isnan(A_s)):
            raise ValueError("Beam interpolation resulted in an invalid value")

    return A_s


def _validate_inputs(precision, polarized, antpos, eq2tops, crd_eq, I_sky):
    assert precision in {1, 2}

    # Specify number of polarizations (axes/feeds)
    if polarized:
        nax = nfeed = 2
    else:
        nax = nfeed = 1

    nant, ncrd = antpos.shape
    assert ncrd == 3, "antpos must have shape (NANTS, 3)."
    ntimes, ncrd1, ncrd2 = eq2tops.shape
    assert ncrd1 == 3 and ncrd2 == 3, "eq2tops must have shape (NTIMES, 3, 3)."
    ncrd, nsrcs = crd_eq.shape
    assert ncrd == 3, "crd_eq must have shape (3, NSRCS)."
    assert (
        I_sky.ndim == 1 and I_sky.shape[0] == nsrcs
    ), "I_sky must have shape (NSRCS,)."

    return nax, nfeed, nant, ntimes


@profile
def vis_cpu(
    *,
    antpos: np.ndarray,
    freq: float,
    eq2tops: np.ndarray,
    crd_eq: np.ndarray,
    I_sky: np.ndarray,
    beam_list: Sequence[UVBeam | Callable] | None,
    precision: int = 1,
    polarized: bool = False,
    beam_idx: np.ndarray | None = None,
    beam_spline_opts: dict | None = None,
):
    """
    Calculate visibility from an input intensity map and beam model.

    Parameters
    ----------
    antpos : array_like
        Antenna position array. Shape=(NANT, 3).
    freq : float
        Frequency to evaluate the visibilities at [GHz].
    eq2tops : array_like
        Set of 3x3 transformation matrices to rotate the RA and Dec
        cosines in an ECI coordinate system (see `crd_eq`) to
        topocentric ENU (East-North-Up) unit vectors at each
        time/LST/hour angle in the dataset.
        Shape=(NTIMES, 3, 3).
    crd_eq : array_like
        Cartesian unit vectors of sources in an ECI (Earth Centered
        Inertial) system, which has the Earth's center of mass at
        the origin, and is fixed with respect to the distant stars.
        The components of the ECI vector for each source are:
        (cos(RA) cos(Dec), sin(RA) cos(Dec), sin(Dec)).
        Shape=(3, NSRCS).
    I_sky : array_like
        Intensity distribution of sources/pixels on the sky, assuming intensity
        (Stokes I) only. The Stokes I intensity will be split equally between
        the two linear polarization channels, resulting in a factor of 0.5 from
        the value inputted here. This is done even if only one polarization
        channel is simulated.
        Shape=(NSRCS,).
    bm_cube : array_like, optional
        Pixelized beam maps for each antenna. If ``polarized=False``,
        shape=``(NBEAMS, BM_PIX, BM_PIX)``, otherwise
        shape=``(NAX, NFEED, NBEAMS, BM_PIX, BM_PIX)``. Only one of ``bm_cube`` and
        ``beam_list`` should be provided. If NBEAMS != NANT, then `beam_idx` must be
        provided also. Note that the projected coordinates corresponding to the bm_cube
        MUST be equivalent to those returned by :func:`~conversions.bm_pix_to_lm`.
    beam_list : list of UVBeam, optional
        If specified, evaluate primary beam values directly using UVBeam
        objects instead of using pixelized beam maps. Only one of ``bm_cube`` and
        ``beam_list`` should be provided.Note that if `polarized` is True,
        these beams must be efield beams, and conversely if `polarized` is False they
        must be power beams with a single polarization (either XX or YY).
    precision : int, optional
        Which precision level to use for floats and complex numbers.
        Allowed values:
        - 1: float32, complex64
        - 2: float64, complex128
    polarized : bool, optional
        Whether to simulate a full polarized response in terms of nn, ne, en,
        ee visibilities. See Eq. 6 of Kohn+ (arXiv:1802.04151) for notation.
        Default: False.
    beam_idx
        Optional length-NANT array specifying a beam index for each antenna.
        By default, either a single beam is assumed to apply to all antennas or
        each antenna gets its own beam.

    Returns
    -------
    vis : array_like
        Simulated visibilities. If `polarized = True`, the output will have
        shape (NTIMES, NFEED, NFEED, NANTS, NANTS), otherwise it will have
        shape (NTIMES, NANTS, NANTS).
    """
    nax, nfeed, nant, ntimes = _validate_inputs(
        precision, polarized, antpos, eq2tops, crd_eq, I_sky
    )

    if precision == 1:
        real_dtype = np.float32
        complex_dtype = np.complex64
    else:
        real_dtype = np.float64
        complex_dtype = np.complex128

    beam_list, nbeam, beam_idx = _wrangle_beams(
        beam_idx, beam_list, polarized, nant, freq, interp=True
    )

    # Intensity distribution (sqrt) and antenna positions. Does not support
    # negative sky. Factor of 0.5 accounts for splitting Stokes I between
    # polarization channels
    Isqrt = np.sqrt(0.5 * I_sky).astype(real_dtype)
    antpos = antpos.astype(real_dtype)

    ang_freq = 2.0 * np.pi * freq

    # Zero arrays: beam pattern, visibilities, delays, complex voltages
    vis = np.zeros((ntimes, nfeed * nant, nfeed * nant), dtype=complex_dtype)
    crd_eq = crd_eq.astype(real_dtype)

    # Loop over time samples
    for t, eq2top in enumerate(eq2tops.astype(real_dtype)):
        # Dot product converts ECI cosines (i.e. from RA and Dec) into ENU
        # (topocentric) cosines, with (tx, ty, tz) = (e, n, u) components
        # relative to the center of the array
        tx, ty, tz = crd_top = np.dot(eq2top, crd_eq)
        above_horizon = tz > 0
        tx = tx[above_horizon]
        ty = ty[above_horizon]
        nsrcs_up = len(tx)
        isqrt = Isqrt[above_horizon]

        v = np.zeros((nant, nsrcs_up), dtype=complex_dtype)

        A_s = _evaluate_beam_cpu(
            beam_list,
            tx,
            ty,
            polarized,
            nbeam,
            nax,
            nfeed,
            freq,
            nsrcs_up,
            complex_dtype,
            spline_opts=beam_spline_opts,
        ).transpose(
            (1, 2, 0, 3)
        )  # Now (Nfeed, Nbeam, Nax, Nsrc)

        logger.debug(
            "CPU: Beam: %s", A_s.flatten() if A_s.size < 40 else A_s.flatten()[:40]
        )

        # Calculate delays, where tau = (b * s) / c
        tau = np.dot(antpos / c.value, crd_top[:, above_horizon])

        logger.debug(
            "CPU: tau: %s %s",
            tau.flatten() if tau.size < 40 else tau.flatten()[:40],
            tau.shape,
        )

        v = get_antenna_vis(
            A_s, ang_freq, tau, isqrt, beam_idx, nfeed, nant, nax, nsrcs_up
        )
        logger.debug(
            "CPU: vant: %s %s",
            v.flatten() if v.size < 40 else v.flatten()[:40],
            v.shape,
        )

        # Compute visibilities using product of complex voltages (upper triangle).
        vis[t] = v.conj().dot(v.T)

        logger.debug(
            "CPU: vis: %s", vis.flatten() if vis.size < 40 else vis.flatten()[:40]
        )

    vis.shape = (ntimes, nfeed, nant, nfeed, nant)

    # Return visibilities with or without multiple polarization channels
    return vis.transpose((0, 1, 3, 2, 4)) if polarized else vis[:, 0, :, 0, :]


def get_antenna_vis(
    A_s, ang_freq, tau, Isqrt, beam_idx, nfeed, nant, nax, nsrcs_up
) -> np.ndarray:
    """Compute the antenna-wise visibility integrand."""
    # Component of complex phase factor for one antenna
    # (actually, b = (antpos1 - antpos2) * crd_top / c; need dot product
    # below to build full phase factor for a given baseline)
    v = np.exp(1.0j * (ang_freq * tau)) * Isqrt

    # A_s has shape (Nax, Nfeed, Nbeams, Nsources)
    # v has shape (Nants, Nsources) and is sqrt(I)*exp(1j tau*nu)
    # Here we expand A_s to all ants (from its beams), then broadcast to v, so we
    # end up with shape (Nax, Nfeed, Nants, Nsources)
    v = A_s[:, beam_idx] * v[np.newaxis, :, np.newaxis, :]  # ^ but Nbeam -> Nant
    return v.reshape((nfeed * nant, nax * nsrcs_up))  # reform into matrix
