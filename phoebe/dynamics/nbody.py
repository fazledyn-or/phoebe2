"""
"""

import numpy as np
from scipy.optimize import newton


from phoebe import u, c
from phoebe.dynamics import keplerian
from phoebe import conf

from distutils.version import LooseVersion

from phoebe.dependencies import photodynam

try:
    import rebound
except ImportError:
    _can_rebound = False
else:
    _can_rebound = LooseVersion(rebound.__version__) >= LooseVersion('3.4.0')

if _can_rebound:
    try:
        import reboundx
    except (ImportError, OSError):
        _can_reboundx = False
    else:
        _can_reboundx = True
else:
    _can_reboundx = False

import logging
logger = logging.getLogger("DYNAMICS.NBODY")
logger.addHandler(logging.NullHandler())


au_to_solrad = (1*u.AU).to(u.solRad).value

def _ensure_tuple(item):
    """
    Simply ensure that the passed item is a tuple.  If it is not, then
    convert it if possible, or raise a NotImplementedError

    Args:
        item: the item that needs to become a tuple

    Returns:
        the item casted as a tuple

    Raises:
        NotImplementedError: if converting the given item to a tuple
            is not implemented.
    """
    if isinstance(item, tuple):
        return item
    elif isinstance(item, list):
        return tuple(item)
    elif isinstance(item, np.ndarray):
        return tuple(item.tolist())
    else:
        raise NotImplementedError

def keplerian_from_cartesian(positions, velocities, mass_tot, t0=0.0, G=c.G.to('solRad3 / (solMass d2)').value):
    """
    positions nparray relative to sibling star in solRad (or pass appropriate G)
    velocities nparray relative to sibling star in solRad/d (or pass appropriate G)
    mass_tot float in solMass (or pass appropriate G)

    using equations from Fundamentals of Astrodynamics and Applications, by Vallado, 2007.
    see: https://space.stackexchange.com/a/1919
    with inspiration from: https://github.com/RazerM/orbital/blob/0.7.0/orbital/utilities.py#L252
    """
    r_v = np.asarray(positions)
    r = np.linalg.norm(r_v)

    v_v = np.asarray(velocities)
    v = np.linalg.norm(v_v)

    mu = mass_tot * G

    h_v = np.cross(r_v, v_v)
    h = np.linalg.norm(h_v)

    n_v = np.cross([0, 0, 1], h_v)
    n = np.linalg.norm(n_v)

    e_v = ((v ** 2 - mu / r) * r_v - np.dot(r_v, v_v) * v_v) / mu
    e = np.linalg.norm(e_v)

    if e >= 1:
        raise NotImplementedError("ecc cannot be >= 1")

    E = v**2 / 2 - mu / r
    a = - mu / (2 * E)

    incl = np.arccos(h_v[2] / h)

    if abs(incl) < 1e-12:
        long_an = 0

        if e == 0.:
            # for circular orbits, per0 is 0 by convention.  This will probably
            # never be exactly zero... but then its up to the user to realize
            # that per0 is meaningless
            per0 = 0
        else:
            per0 = np.arccos(e_v[0] / e)

    else:
        long_an = np.arccos(n_v[0] / n)
        if n_v[1] < 0:
            long_an = 2*np.pi - long_an

        # TODO: figure out why long_an needs to be adjusted by pi... possibly
        # due to coordinate system conventions
        long_an += np.pi

        if e == 0.:
            per0 = 0
        else:
            per0 = np.arccos(np.dot(n_v, e_v) / (n * e))

    # when running keplerian intial pos/vel with e=0, we get e=1e-8 back and
    # we want to make sure we still trigger the special case in those scenarios
    if e < 1e-6:
        if abs(incl) < 1e-12:
            true_anom = np.arccos(r_v[0] / r)
            if v_v[0] > 0:
                true_anom = 2*np.pi - true_anom

        else:
            true_anom = np.arccos(np.dot(n_v, r_v) / (n*r))
            if np.dot(n_v, v_v) > 0:
                true_anom = 2*np.pi - true_anom
    else:
        if e_v[2] < 0 and e > 0.:
            per0 = 2*np.pi - per0

        true_anom = np.arccos(np.dot(e_v, r_v) / (e * r))

        if np.dot(r_v, v_v) < 0:
            true_anom = 2*np.pi - true_anom

    # compute period from kepler's third law, the determined value of a,
    # and the fixed/provided values for mass_tot and G
    period = ((4 * np.pi**2 * a**3)/(mass_tot * G))**0.5

    # convert from true anom to period and t0_perpass
    mean_anom = true_anom - e * np.sin(true_anom)
    t0_perpass = t0 - (mean_anom * period) / (2*np.pi)


    # sma (solRad if G not provided), ecc, period (d if G not provided), incl (rad), per0 (rad), long_an (rad), mean_anom (rad), t0_perpass (rad)
    return a, e, period, incl, per0, long_an, true_anom, mean_anom, t0_perpass




def dynamics_from_bundle(b, times, compute=None, return_roche_euler=False, **kwargs):
    return dynamics_from_bundle_bs(b, times, compute=compute, return_roche_euler=return_roche_euler, **kwargs)

def dynamics_from_bundle_rebound(b, times, compute=None, return_roche_euler=False, **kwargs):
    """
    Parse parameters in the bundle and call :func:`dynamics`.

    See :func:`dynamics` for more detailed information.

    NOTE: you must either provide compute (the label) OR all relevant options
    as kwargs (ltte, stepsize, gr, integrator)

    Args:
        b: (Bundle) the bundle with a set hierarchy
        times: (list or array) times at which to run the dynamics
        stepsize: (float, optional) stepsize for the integration
            [default: 0.01]
        orbiterror: (float, optional) orbiterror for the integration
            [default: 1e-16]
        ltte: (bool, default False) whether to account for light travel time effects.
        gr: (bool, default False) whether to account for general relativity effects.

    Returns:
        t, xs, ys, zs, vxs, vys, vzs.  t is a numpy array of all times,
        the remaining are a list of numpy arrays (a numpy array per
        star - in order given by b.hierarchy.get_stars()) for the cartesian
        positions and velocities of each star at those same times.

    """

    # TODO: need to make a non-bundle version of this

    b.run_delayed_constraints()

    hier = b.hierarchy

    computeps = b.get_compute(compute, check_visible=False, force_ps=True)
    stepsize = computeps.get_value('stepsize', check_visible=False, **kwargs)
    ltte = computeps.get_value('ltte', check_visible=False, **kwargs)
    gr = computeps.get_value('gr', check_visible=False, **kwargs)
    integrator = computeps.get_value('integrator', check_visible=False, **kwargs)

    logger.debug("nbody.dynamics_from_bundle stepsize={} ltte={} gr={} integrator={}".format(stepsize, ltte, gr, integrator))

    starrefs = hier.get_stars()
    orbitrefs = hier.get_orbits()

    # vgamma = b.get_value('vgamma', context='system', unit=u.AU/u.d) # should be extracted by keplerian.dynamics_from_bundle
    t0 = b.get_value('t0', context='system', unit=u.d)

    if not _can_rebound:
        raise ImportError("rebound 3.4+ is not installed")

    if gr and not _can_reboundx:
        raise ImportError("reboundx is not installed (required for gr effects)")

    dump, dump, xs, ys, zs, vxs, vys, vzs = keplerian.dynamics_from_bundle(b, [t0], compute=compute)
    logger.debug("nbody.dynamics_from_bundle, intial values at t0: xs={}, ys={}, zs={}, vxs={}, vys={}, vzs={}".format([x[0] for x in xs], [y[0] for y in ys], [z[0] for z in zs], [vx[0] for vx in vxs], [vy[0] for vy in vys], [vz[0] for vz in vzs]))

    def particle_ltte(sim, particle_N, t_obs):
        c_AU_d = c.c.to(u.AU/u.d).value

        def residual(t):
            # print "*** ltte trying t:", t
            if sim.t != t:
                sim.integrate(t, exact_finish_time=True)
            ltte_dt = sim.particles[particle_N].z / c_AU_d
            t_barycenter = t - ltte_dt
            # print "***** ", t_barycenter-t_obs

            return t_barycenter - t_obs

        t_barycenter = newton(residual, t_obs)

        if sim.t != t_barycenter:
            sim.integrate(t_barycenter)

        return sim.particles[particle_N]

    def com_phantom_particle(particles):
        m = sum([p.m for p in particles])
        x = 1./m * sum([p.m*p.x for p in particles])
        y = 1./m * sum([p.m*p.y for p in particles])
        z = 1./m * sum([p.m*p.z for p in particles])
        vx = 1./m * sum([p.m*p.vx for p in particles])
        vy = 1./m * sum([p.m*p.vy for p in particles])
        vz = 1./m * sum([p.m*p.vz for p in particles])

        return rebound.Particle(m=m, x=x, y=y, z=z, vx=vx, vy=vy, vz=vz)

    def calculate_euler(sim, j):
        # NOTE: THIS WILL FAIL FOR j==0 FOR REBOUND < 3.3.2
        particle = sim.particles[j]

        sibling_particles = [sim.particles[k] for k in sibling_Ns[j]]
        # NOTE: this isn't the com of THIS system, but rather the COM
        # of the sibling (if it consists of more than 1 star)
        com_particle = com_phantom_particle(sibling_particles)
        # com_particle = com_phantom_particle([particle]+sibling_particles)

        # get the orbit based on this com_particle as the primary component
        orbit = particle.calculate_orbit(primary=com_particle)

        # print "*** m1 m2: {} {}".format(particle.m, com_particle.m)
        # print "*** (x1, y1, z1) (x2, y2, z2): ({}, {}, {}) ({}, {}, {})".format(particle.x, particle.y, particle.z, com_particle.x, com_particle.y, com_particle.z)
        # print "*** (vx1, vy1, vz1) (vx2, vy2, vz2): ({}, {}, {}) ({}, {}, {})".format(particle.vx, particle.vy, particle.vz, com_particle.vx, com_particle.vy, com_particle.vz)
        # print "**** P, a, d, d/a, F, inc", orbit.P, orbit.a, orbit.d, orbit.d/orbit.a, orbit.P/rotperiods[j], orbit.inc

        # for instantaneous separation, we need the current separation
        # from the sibling component in units of its instantaneous (?) sma
        d = orbit.d / orbit.a
        # for syncpar (F), assume that the rotational FREQUENCY will
        # remain fixed - so we simply need to updated syncpar based
        # on the INSTANTANEOUS orbital PERIOD.
        F = orbit.P / rotperiods[j]

        # TODO: need to add np.pi for secondary component?
        etheta = orbit.f + orbit.omega + np.pi # true anomaly + periastron

        elongan = orbit.Omega - np.pi # TODO: need to check if this is correct, this set to orbit.Omega seemed to be causing the offset issue

        eincl = orbit.inc

        # print "*** calculate_euler ind: {}, etheta: {} [{}] ({}, {}), sibling_inds: {}, self: ({}, {}, {}), sibling: ({}, {}, {})".format(j, etheta, etheta+np.pi if etheta<np.pi else etheta-np.pi, orbit.f, orbit.omega, sibling_Ns[j], particle.x, particle.y, particle.z, com_particle.x, com_particle.y, com_particle.z)

        period = orbit.P
        sma = orbit.a
        ecc = orbit.e
        per0 = orbit.omega
        long_an = orbit.Omega - np.pi
        incl = orbit.inc
        t0_perpass = orbit.T

        # TODO: all after eincl are only required if requesting to store in the
        # mesh... but shouldn't be taking too much time to calculate and store
        return d, F, etheta, elongan, eincl, period, sma, ecc, per0, long_an, incl, t0_perpass

    times = np.asarray(times) - t0

    sim = rebound.Simulation()

    if gr:
        logger.info("nbody.dynamics_from_bundle: enabling 'gr_full' in reboundx")
        rebx = reboundx.Extras(sim)
        # TODO: switch between different GR setups based on masses/hierarchy
        # http://reboundx.readthedocs.io/en/latest/effects.html#general-relativity
        params = rebx.add_gr_full()

    sim.integrator = integrator
    # NOTE: according to rebound docs: "stepsize will change for adaptive
    # integrators such as IAS15"
    sim.dt = stepsize

    sibling_Ns = []
    for i,starref in enumerate(starrefs):
        # print "***", i, starref

        # TODO: do this in rsol instead of AU so we don't have to convert the particles back and forth below?
        # mass = b.get_value('mass', u.solMass, component=starref, context='component') * c.G.to('AU3 / (Msun d2)').value
        mass = b.get_value('mass', u.solMass, component=starref, context='component') * c.G.to('solRad3 / (Msun d2)').value

        if return_roche_euler:
            # rotperiods are only needed to compute instantaneous syncpars
            rotperiods = [b.get_value('period', u.d, component=component, context='component') for component in starrefs]
        else:
            rotperiod = None


        # xs, ys, zs are in solRad; vxs, vys, vzs are in solRad/d
        # pass to rebound in AU and AU/d
        logger.debug("nbody.dynamics_from_bundle: starref={} sim.add(m={}, x={}, y={}, z={}, vx={}, vy={}, vz={})".format(starref, mass, xs[i][0]/au_to_solrad, ys[i][0]/au_to_solrad, zs[i][0]/au_to_solrad, vxs[i][0]/au_to_solrad, vys[i][0]/au_to_solrad, vzs[i][0]/au_to_solrad))
        # sim.add(m=mass, x=xs[i][0]/au_to_solrad, y=ys[i][0]/au_to_solrad, z=zs[i][0]/au_to_solrad, vx=vxs[i][0]/au_to_solrad, vy=vys[i][0]/au_to_solrad, vz=vzs[i][0]/au_to_solrad)
        sim.add(m=mass, x=xs[i][0], y=ys[i][0], z=zs[i][0], vx=vxs[i][0], vy=vys[i][0], vz=vzs[i][0])

        # also track the index of all particles that need to be included as
        # the sibling of this particle (via their COM)
        # TODO: only do this if return_roche_euler?
        sibling_starrefs = hier.get_stars_of_sibling_of(starref)
        sibling_Ns.append([starrefs.index(s) for s in sibling_starrefs])


    #### TESTING ###
    # print "*** TESTING EULER BEFORE INTEGRATION"
    # for j,starref in enumerate(starrefs):
    #     print "*** {} original: (x, y, z) (vx, vy, vz): ({}, {}, {}) ({}, {}, {})".format(starref, xs[j][0]/au_to_solrad, ys[j][0]/au_to_solrad, zs[j][0]/au_to_solrad, vxs[j][0]/au_to_solrad, vys[j][0]/au_to_solrad, vzs[j][0]/au_to_solrad)
    #     calculate_euler(sim, j)
    # exit()

    ################



    xs = [np.zeros(times.shape) for j in range(sim.N)]
    ys = [np.zeros(times.shape) for j in range(sim.N)]
    zs = [np.zeros(times.shape) for j in range(sim.N)]
    vxs = [np.zeros(times.shape) for j in range(sim.N)]
    vys = [np.zeros(times.shape) for j in range(sim.N)]
    vzs = [np.zeros(times.shape) for j in range(sim.N)]

    if return_roche_euler:
        # from instantaneous Keplerian dynamics for Roche meshing
        ds = [np.zeros(times.shape) for j in range(sim.N)]
        Fs = [np.zeros(times.shape) for j in range(sim.N)]

        ethetas = [np.zeros(times.shape) for j in range(sim.N)]
        elongans = [np.zeros(times.shape) for j in range(sim.N)]
        eincls = [np.zeros(times.shape) for j in range(sim.N)]

        periods = [np.zeros(times.shape) for j in range(sim.N)]
        smas = [np.zeros(times.shape) for j in range(sim.N)]
        eccs = [np.zeros(times.shape) for j in range(sim.N)]
        per0s = [np.zeros(times.shape) for j in range(sim.N)]
        long_ans = [np.zeros(times.shape) for j in range(sim.N)]
        incls = [np.zeros(times.shape) for j in range(sim.N)]
        t0_perpasses = [np.zeros(times.shape) for j in range(sim.N)]

    for i,time in enumerate(times):

        # print "*** integrating to t=", time

        sim.integrate(time, exact_finish_time=True)

        # if return_roche:
            # TODO: do we need to do this after handling LTTE???
            # orbits = sim.calculate_orbits()

        for j in range(sim.N):

            # get roche/euler information BEFORE making LTTE adjustments
            if return_roche_euler:


                d, F, etheta, elongan, eincl, period, sma, ecc, per0, long_an, incl, t0_perpass = calculate_euler(sim, j)

                ds[j][i] = d
                Fs[j][i] = F
                ethetas[j][i] = etheta
                elongans[j][i] = elongan
                eincls[j][i] = eincl

                periods[j][i] = period
                smas[j][i] = sma
                eccs[j][i] = ecc
                per0s[j][i] = per0
                long_ans[j][i] = long_an
                incls[j][i] = incl
                t0_perpasses[j][i] = t0_perpass

            # if necessary, make adjustments to particle for LTTE and then
            # store position/velocity of the particle
            if ltte:
                # then we need to integrate to different times per object
                particle = particle_ltte(sim, j, time)
            else:
                # print "***", time, j, sim.N
                particle = sim.particles[j]

            # xs[j][i] = particle.x * au_to_solrad # solRad
            # ys[j][i] = particle.y * au_to_solrad # solRad
            # zs[j][i] = particle.z * au_to_solrad  # solRad
            # vxs[j][i] = particle.vx * au_to_solrad # solRad/d
            # vys[j][i] = particle.vy * au_to_solrad # solRad/d
            # vzs[j][i] = particle.vz * au_to_solrad # solRad/d
            xs[j][i] = particle.x # solRad
            ys[j][i] = particle.y # solRad
            zs[j][i] = particle.z # solRad
            vxs[j][i] = particle.vx # solRad/d
            vys[j][i] = particle.vy # solRad/d
            vzs[j][i] = particle.vz # solRad/d


    if return_roche_euler:
        # d, solRad, solRad/d, rad, unitless (sma), unitless, rad, rad, rad
        return times, xs, ys, zs, vxs, vys, vzs, ds, Fs, ethetas, elongans, eincls, periods, smas, eccs, per0s, long_ans, incls, t0_perpasses

    else:
        # d, solRad, solRad/d, rad
        return times, xs, ys, zs, vxs, vys, vzs


def dynamics_from_bundle_bs(b, times, compute=None, return_roche_euler=False, **kwargs):
    """
    Parse parameters in the bundle and call :func:`dynamics`.

    See :func:`dynamics` for more detailed information.

    NOTE: you must either provide compute (the label) OR all relevant options
    as kwargs (ltte)

    Args:
        b: (Bundle) the bundle with a set hierarchy
        times: (list or array) times at which to run the dynamics
        stepsize: (float, optional) stepsize for the integration
            [default: 0.01]
        orbiterror: (float, optional) orbiterror for the integration
            [default: 1e-16]
        ltte: (bool, default False) whether to account for light travel time effects.

    Returns:
        t, xs, ys, zs, vxs, vys, vzs.  t is a numpy array of all times,
        the remaining are a list of numpy arrays (a numpy array per
        star - in order given by b.hierarchy.get_stars()) for the cartesian
        positions and velocities of each star at those same times.

    """

    stepsize = 0.01
    orbiterror = 1e-16
    computeps = b.get_compute(compute, check_visible=False, force_ps=True)
    ltte = computeps.get_value('ltte', check_visible=False, **kwargs)


    hier = b.hierarchy

    starrefs = hier.get_stars()
    orbitrefs = hier.get_orbits()

    masses = [b.get_value('mass', u.solMass, component=component, context='component') * c.G.to('AU3 / (Msun d2)').value for component in starrefs]  # GM
    smas = [b.get_value('sma', u.AU, component=component, context='component') for component in orbitrefs]
    eccs = [b.get_value('ecc', component=component, context='component') for component in orbitrefs]
    incls = [b.get_value('incl', u.rad, component=component, context='component') for component in orbitrefs]
    per0s = [b.get_value('per0', u.rad, component=component, context='component') for component in orbitrefs]
    long_ans = [b.get_value('long_an', u.rad, component=component, context='component') for component in orbitrefs]
    t0_perpasses = [b.get_value('t0_perpass', u.d, component=component, context='component') for component in orbitrefs]
    periods = [b.get_value('period', u.d, component=component, context='component') for component in orbitrefs]

    vgamma = b.get_value('vgamma', context='system', unit=u.solRad/u.d)
    t0 = b.get_value('t0', context='system', unit=u.d)

    mean_anoms = [b.get_value('mean_anom', u.rad, component=component, context='component') for component in orbitrefs]

    return dynamics_bs(times, masses, smas, eccs, incls, per0s, long_ans, \
                    mean_anoms, t0, vgamma, stepsize, orbiterror, ltte,
                    return_roche_euler=return_roche_euler)




def dynamics_bs(times, masses, smas, eccs, incls, per0s, long_ans, mean_anoms,
        t0=0.0, vgamma=0.0, stepsize=0.01, orbiterror=1e-16, ltte=False,
        return_roche_euler=False):
    """
    Burlisch-Stoer integration of orbits to give positions and velocities
    of any given number of stars in hierarchical orbits.  This code
    currently uses the NBody code in Josh Carter's photodynam code
    available here:

    [github.com/phoebe-project/photodynam](http://github.com/phoebe-project/photodynam)

    If using the Nbody mode in PHOEBE, please cite him as well:

    * Science 4 February 2011: Vol. 331 no. 6017 pp. 562-565 DOI:10.1126/science.1201274
    * MNRAS (2012) 420 (2): 1630-1635. doi: 10.1111/j.1365-2966.2011.20151.x

    See <phoebe.dynamics.nbody.dynamics_from_bundle_bs> for a wrapper around this function
    which automatically handles passing everything in the correct order
    and in the correct units.

    For each iterable input, stars and orbits should be listed in order
    from primary -> secondary for each nested hierarchy level.  Each
    iterable for orbits should have length one less than those for each
    star (ie if 3 masses are provided, then 2 smas, eccs, etc need to
    be provided)

    Args:
        times: (iterable) times at which to compute positions and
            velocities for each star
        masses: (iterable) mass for each star in [GM - solRad3/d2]
        smas: (iterable) semi-major axis for each orbit [solRad]
        eccs: (iterable) eccentricities for each orbit
        incls: (iterable) inclinations for each orbit [rad]
        per0s: (iterable) longitudes of periastron for each orbit [rad]
        long_ans: (iterable) longitudes of the ascending node for each
            orbit [rad]
        mean_anoms: (iterable) mean anomalies for each orbit
        t0: (float) time at which to start the integrations
        stepsize: (float, optional) stepsize of the integrations
            [default: 0.01]
        orbiterror: (float, optional) orbiterror of the integrations
            [default: 1e-16]
        ltte: (bool, default False) whether to account for light travel time effects.

    Returns:
        t, xs, ys, zs, vxs, vys, vzs.  t is a numpy array of all times,
        the remaining are a list of numpy arrays (a numpy array per
        star - in order given by b.hierarchy.get_stars()) for the cartesian
        positions and velocities of each star at those same times.

    """

    times = np.asarray(times)
    masses = np.asarray(masses)
    smas = np.asarray(smas)
    eccs = np.asarray(eccs)
    incls = np.asarray(incls)
    per0s = np.asarray(per0s)
    long_ans = np.asarray(long_ans)
    mean_anoms = np.asarray(mean_anoms)

    # TODO: include vgamma (see https://github.com/phoebe-project/photodynam/issues/2)
    logger.debug("calling photodynam.do_dynamics(times={}, masses={}, smas={}, eccs={}, incls={}, per0s={}, long_ans={}, mean_anoms={}, t0={}, stepsize={}, orbiterror={}, ltte={}, return_roche_euler={})".format(times, masses, smas, eccs, incls, per0s, long_ans, mean_anoms, t0, stepsize, orbiterror, ltte, return_roche_euler))
    d = photodynam.do_dynamics(times, masses, smas, eccs, incls, per0s, long_ans, mean_anoms, t0, stepsize, orbiterror, ltte, return_roche_euler)
    # d is in the format: {'t': (t0, t1, ...), 'x': ( (t0_comp1,t0_comp2,t0_comp3), (t1_comp1,t1_comp2,t1_comp3), ...), 'y': ..., 'z': ...}
    # d.keys(): ['kepl_a', 'kepl_e', 'kepl_o', 'kepl_in', 'kepl_ma', 'vx', 'kepl_ln', 't', 'vy', 'y', 'x', 'vz', 'z']

    nobjects = len(masses)
    norbits = len(smas)
    ntimes = len(times)


    ts = d['t']
    # we need to transpose the output from photodynam so that the first index
    # is the component and the second is the time.
    xs = -1*d['x'].T * au_to_solrad
    ys = -1*d['y'].T * au_to_solrad
    zs = d['z'].T * au_to_solrad
    vxs = -1*d['vx'].T * au_to_solrad
    vys = -1*d['vy'].T * au_to_solrad
    vzs = d['vz'].T * au_to_solrad

    if return_roche_euler:
        def _compute_period(sma, mass_tot, G=1):
            return ((4 * np.pi**2 * (sma/au_to_solrad)**3)/(mass_tot * G))**0.5
        def _compute_t0_perpass(mean_anom, period, t0=0.0):
            return t0 - (mean_anom * period) / (2*np.pi)

        # TODO: can this be cleaned up to use the transpose and just add an entry at the beginning/end
        # TODO: will this work for all hierarchy orders/arrangements?
        # kepl_a (sma), kepl_e (ecc), kepl_in (incl), kepl_o (per0), kepl_ln (long_an), kepl_ma (mean_anom)
        inst_smas = [(np.array([d['kepl_a'][ti][oi if oi==0 else oi-1] for ti in range(ntimes)]))*au_to_solrad for oi in range(nobjects)]
        inst_eccs = [(np.array([d['kepl_e'][ti][oi if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]
        inst_incls = [(np.array([d['kepl_in'][ti][oi if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]
        inst_per0s = [(np.array([d['kepl_o'][ti][oi if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]
        inst_long_ans = [(np.array([d['kepl_ln'][ti][oi if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]
        inst_mean_anoms = [(np.array([d['kepl_ma'][ti][oi if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]

        # compute period and t0_perpass from kepler's third law and mean anomaly
        mass_tots = [np.sum(masses[:2 if oi==0 else oi+1]) for oi in range(nobjects)]
        inst_periods = [_compute_period(smas, mass_tot) for smas, mass_tot in zip(inst_smas, mass_tots)]
        # TODO: need to pass t0@system
        inst_t0_perpasses = [_compute_t0_perpass(mean_anoms, periods) for mean_anoms, periods in zip(inst_mean_anoms, inst_periods)]

        # TODO: actually compute d from the positions of the sibling component
        inst_ds = [(np.array([None for ti in range(ntimes)])) for oi in range(nobjects)]

        # TODO: update this from orbital and rotation periods
        inst_Fs = [(np.array([None for ti in range(ntimes)])) for oi in range(nobjects)]

        # TODO: check to make sure these are the right angles
        # TODO: need to add np.pi for secondary component?
        inst_ethetas = [(np.array([d['kepl_o'][ti][oi if oi==0 else oi-1]+d['kepl_ma'][ti][oi if oi==0 else oi-1]+np.pi/2 for ti in range(ntimes)])) for oi in range(nobjects)]
        inst_elongans = [(np.array([d['kepl_ln'][ti][oi if oi==0 else oi-1]+long_ans[0 if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]
        inst_eincls = [(np.array([d['kepl_in'][ti][oi if oi==0 else oi-1]+np.pi-incls[0 if oi==0 else oi-1] for ti in range(ntimes)])) for oi in range(nobjects)]


        # d, solRad [3], solRad/d [3], solRad, unitless, rad [3], ...
        return ts, xs, ys, zs, vxs, vys, vzs, inst_ds, inst_Fs, inst_ethetas, inst_elongans, inst_eincls, inst_periods, inst_smas, inst_eccs, inst_per0s, inst_long_ans, inst_incls, inst_t0_perpasses

    else:
        # d, solRad [3], solRad/d [3]
        return ts, xs, ys, zs, vxs, vys, vzs
